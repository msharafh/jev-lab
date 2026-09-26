"""Jev capture triage: file bookmarks into a knowledge vault with typed judgements.

For each capture (a saved post, article or note), ask Jev (TypeSafe's System One
model) which vault page it belongs to, how substantive it is, and whether it is
promotion. If captures already carry `**Related:** [[Page]]` links (for example
from a keyword tagger), Jev's links are compared against them.

Two requests per capture at most (pattern from TypeSafe's skill-suggestion cookbook):
  Stage 1 - one call, three parallel questions over the capture:
      page       Choice  over every catalog page + `none`
      substance  Score   0-3, hype -> dense reusable knowledge
      promo      Noul    primarily promotion with unverifiable claims?
  Stage 2 - only if `none` is not dominant: one Noul per top-3 candidate,
      "does this capture add specific information that belongs on page X?"
      Every candidate with fit >= --fit becomes a link.

Usage:
    export TYPESAFE_API_KEY=...            # from https://console.typesafe.ai/
    python triage.py --mock --limit 20     # offline dry run, no key needed
    python triage.py --limit 20            # small live run
    python triage.py --set all             # every capture

Data: uses ./captures and ./catalog.json when present, otherwise the bundled
./demo data. Override with --captures / --catalog or JEV_LAB_CAPTURES / JEV_LAB_CATALOG.
Describe your vault to Jev with JEV_LAB_VAULT_TOPIC, e.g. "AI engineering and investing".
"""
import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, RetryPolicy, Score, TypeSafeError

HERE = Path(__file__).parent
NONE = "none"


def data_path(env: str, own: str, demo: str) -> Path:
    """Your data if present (env var, then ./own), otherwise the bundled demo."""
    if os.environ.get(env):
        return Path(os.environ[env]).expanduser()
    return HERE / own if (HERE / own).exists() else HERE / "demo" / demo


CAPTURES_DIR = data_path("JEV_LAB_CAPTURES", "captures", "captures")
CATALOG_FILE = data_path("JEV_LAB_CATALOG", "catalog.json", "catalog.json")
VAULT_TOPIC = os.environ.get("JEV_LAB_VAULT_TOPIC", "").strip()
PRICE_PER_M_INPUT = 0.042  # USD, jev-1.13 (docs.typesafe.ai/models)

SUBSTANCE_LEVELS = [
    "Pure promotion, hype or engagement bait; nothing a reader could reuse",
    "Mostly opinion or an announcement, with at most one reusable fact or pointer",
    "Useful information: a concrete technique, tool, data point or argument",
    "Dense reusable knowledge: a detailed method, evidence, or a primary source",
]

# --------------------------------------------------------------------------- captures

URL_RE = re.compile(r"(obsidian://\S+|https?://\S+)")
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MDLINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
RELATED_RE = re.compile(r"^\*\*Related:\*\*(.*)$", re.M)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")


def parse_capture(path: Path, root: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    fm, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line and not line.startswith(" "):
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip().strip('"')
            body = text[end + 4:]

    # gold labels = links written by the existing keyword pipeline; removed from the state
    gold = sorted({w.strip() for m in RELATED_RE.findall(body) for w in WIKILINK_RE.findall(m)})
    body = RELATED_RE.sub("", body)
    # Strip obsidian:// URIs, all URLs and Obsidian webview link text so they cannot sway the judgement
    body = body.replace("[Open in Obsidian Webview]", "")
    body = IMG_RE.sub("", body)
    body = MDLINK_RE.sub(r"\1", body)
    body = URL_RE.sub("", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    rel = path.relative_to(root)
    return {
        "id": str(rel),
        "source": rel.parts[0],
        "author": fm.get("author", ""),
        "title": fm.get("title", "") or re.sub(r"^\d{4}-\d{2}-\d{2}-[^-]*-", "", path.stem),
        "published": (fm.get("published", "") or fm.get("created", ""))[:10],
        "text": body[:6000],
        "gold": gold,
    }


def load_captures(root: Path) -> list[dict]:
    return [parse_capture(p, root) for p in sorted(root.rglob("*.md"))]


# --------------------------------------------------------------------------- questions

def page_description(p: dict) -> str:
    d = p["blurb"]
    if p.get("purpose") and p["purpose"].lower() not in d.lower():
        d += f". {p['purpose']}"
    return d


def stage1_questions(catalog: list[dict]) -> dict:
    criteria = {p["name"]: page_description(p) for p in catalog}
    criteria[NONE] = "No page in the vault is a clear topical fit for this capture."
    return {
        "page": Choice(
            instructions=(
                "The capture in `capture` was saved into a personal knowledge vault"
                + (f" about {VAULT_TOPIC}" if VAULT_TOPIC else "")
                + ". Which vault page does this capture "
                "most directly add knowledge to? Choose the page whose subject the capture is "
                "actually about, not one it only mentions in passing. Choose `none` if no page fits."
            ),
            criteria=criteria,
        ),
        "substance": Score(
            instructions="How much reusable knowledge does the capture in `capture` contain?",
            criteria=SUBSTANCE_LEVELS,
        ),
        "promo": Noul(
            instructions=(
                "Is the capture in `capture` primarily promoting a product, token, course, "
                "giveaway or referral, with claims the reader cannot verify?"
            ),
        ),
    }


def stage2_questions(candidates: list[dict]) -> dict:
    return {
        f"fit_{i}": Noul(
            instructions=(
                f"Does the capture in `capture` add specific information about the subject of the "
                f"vault page described in `candidates[{i}]` (\"{c['name']}\") - information that "
                f"would belong on that page, rather than a passing mention?"
            )
        )
        for i, c in enumerate(candidates)
    }


def capture_state(c: dict) -> dict:
    return {"capture": {k: c[k] for k in ("source", "author", "title", "published", "text")}}


# --------------------------------------------------------------------------- run one

async def triage_one(client, c: dict, catalog: list[dict], by_name: dict, args) -> dict:
    t0 = time.perf_counter()
    state = capture_state(c)
    r1 = await client.system_one(state, stage1_questions(catalog))
    page = r1.choices["page"]
    probs = page.probabilities
    ranked = sorted(((n, p) for n, p in probs.items() if n != NONE), key=lambda x: -x[1])
    top3 = ranked[: args.shortlist]
    tokens = r1.usage.input_tokens

    fits = {}
    if probs.get(NONE, 0.0) < args.none_skip and top3:
        cands = [by_name[n] for n, _ in top3]
        state2 = dict(state, candidates=[
            {"name": x["name"], "description": page_description(x), "sections": x["headings"]}
            for x in cands
        ])
        r2 = await client.system_one(state2, stage2_questions(cands))
        fits = {cands[i]["name"]: r2.nouls[f"fit_{i}"].noul for i in range(len(cands))}
        tokens += r2.usage.input_tokens

    return {
        "id": c["id"], "source": c["source"], "title": c["title"][:140], "gold": c["gold"],
        "choice": page.choice, "choice_conf": round(page.confidence, 4),
        "p_none": round(probs.get(NONE, 0.0), 4),
        "top3": [[n, round(p, 4)] for n, p in top3],
        "top": [[n, round(p, 4)] for n, p in sorted(probs.items(), key=lambda x: -x[1])[:6]],
        "sub_probs": {k: round(v, 4) for k, v in r1.scores["substance"].probabilities.items()},
        "fits": {n: round(v, 4) for n, v in fits.items()},
        "substance": round(r1.scores["substance"].score, 3),
        "promo": round(r1.nouls["promo"].noul, 4),
        "input_tokens": tokens,
        "latency_s": round(time.perf_counter() - t0, 3),
    }


# --------------------------------------------------------------------------- evaluation

def links_at(r: dict, fit: float) -> set:
    return {n for n, v in r["fits"].items() if v >= fit}


def evaluate(rows: list[dict], catalog_names: set, fit: float) -> dict:
    labeled = [r for r in rows if r["gold"]]
    out = {"n": len(rows), "n_labeled": len(labeled)}
    if labeled:
        gold_in_cat = [set(r["gold"]) & catalog_names for r in labeled]
        out["gold_outside_catalog"] = sum(len(set(r["gold"]) - catalog_names) for r in labeled)
        out["top1_hit"] = sum(r["choice"] in g for r, g in zip(labeled, gold_in_cat)) / len(labeled)
        out["top3_hit"] = sum(bool({n for n, _ in r["top3"]} & g)
                              for r, g in zip(labeled, gold_in_cat)) / len(labeled)
        sweep = []
        for t in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            tp = fp = fn = 0
            for r, g in zip(labeled, gold_in_cat):
                pred = links_at(r, t)
                tp += len(pred & g); fp += len(pred - g); fn += len(g - pred)
            p = tp / (tp + fp) if tp + fp else 0.0
            rc = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
            sweep.append({"fit": t, "precision": p, "recall": rc, "f1": f1, "tp": tp, "fp": fp, "fn": fn})
        out["sweep"] = sweep
    unl = [r for r in rows if not r["gold"]]
    out["n_unlabeled"] = len(unl)
    out["unlabeled_with_link"] = sum(bool(links_at(r, fit)) for r in unl)
    out["new_links_by_page"] = Counter(n for r in unl for n in links_at(r, fit)).most_common(15)
    out["tokens"] = sum(r["input_tokens"] for r in rows)
    out["cost_usd"] = out["tokens"] / 1e6 * PRICE_PER_M_INPUT
    lat = sorted(r["latency_s"] for r in rows)
    out["latency_p50"] = lat[len(lat) // 2] if lat else 0
    out["latency_p90"] = lat[int(len(lat) * 0.9)] if lat else 0
    return out


def fmt_pct(x):
    return f"{100 * x:.0f}%"


def write_report(run_dir: Path, rows: list[dict], ev: dict, args, catalog_names: set):
    L = [f"# Jev capture triage — run {run_dir.name}", ""]
    L += [f"- Captures: **{ev['n']}** ({ev['n_labeled']} with existing Related links, "
          f"{ev['n_unlabeled']} without)",
          f"- Model: `{args.model}`{' (MOCK — numbers are meaningless)' if args.mock else ''}",
          f"- Input tokens: {ev['tokens']:,} → **${ev['cost_usd']:.3f}**",
          f"- Latency per capture (both stages): p50 {ev['latency_p50']}s · p90 {ev['latency_p90']}s",
          f"- Link threshold `--fit {args.fit}`", ""]

    if ev["n_labeled"]:
        L += ["## Agreement with the existing keyword links", "",
              "> The existing links came from a keyword classifier, so they are a baseline, not truth. "
              "Disagreements below are where one of the two is wrong — worth reading both directions.", "",
              f"- Stage-1 top choice is one of the existing links: **{fmt_pct(ev['top1_hit'])}**",
              f"- An existing link is in Jev's top-3 shortlist: **{fmt_pct(ev['top3_hit'])}**",
              f"- Existing links pointing at pages outside the catalog: {ev['gold_outside_catalog']}", "",
              "| fit ≥ | precision | recall | F1 | agree | Jev-only | keyword-only |",
              "|---|---|---|---|---|---|---|"]
        for s in ev["sweep"]:
            L.append(f"| {s['fit']} | {fmt_pct(s['precision'])} | {fmt_pct(s['recall'])} | "
                     f"{fmt_pct(s['f1'])} | {s['tp']} | {s['fp']} | {s['fn']} |")
        L.append("")

    L += ["## Captures with no existing link", "",
          f"- Jev proposes at least one link for **{ev['unlabeled_with_link']} / {ev['n_unlabeled']}**", ""]
    if ev["new_links_by_page"]:
        L += ["| page | proposed new links |", "|---|---|"]
        L += [f"| [[{n}]] | {k} |" for n, k in ev["new_links_by_page"]]
        L.append("")

    buckets = Counter(min(3, int(r["substance"] + 0.5)) for r in rows)
    L += ["## Substance and promotion", "",
          "| substance level | captures |", "|---|---|"]
    L += [f"| {i} — {SUBSTANCE_LEVELS[i]} | {buckets.get(i, 0)} |" for i in range(4)]
    promo = [r for r in rows if r["promo"] >= 0.5]
    L += ["", f"- Flagged as promotion (p ≥ 0.5): **{len(promo)} / {len(rows)}**"]
    by_page = defaultdict(list)
    for r in rows:
        for g in r["gold"]:
            by_page[g].append(r["promo"])
    top = sorted(((n, sum(v) / len(v), len(v)) for n, v in by_page.items() if len(v) >= 5),
                 key=lambda x: -x[1])[:8]
    if top:
        L += ["", "Mean promotion probability by existing page (≥5 captures):", "",
              "| page | mean p(promo) | captures |", "|---|---|---|"]
        L += [f"| [[{n}]] | {m:.2f} | {k} |" for n, m, k in top]
    L.append("")

    labeled = [r for r in rows if r["gold"]]
    jev_only = [(r, links_at(r, args.fit) - set(r["gold"])) for r in labeled]
    kw_only = [(r, (set(r["gold"]) & catalog_names) - links_at(r, args.fit)) for r in labeled]
    for title, pairs in (("Jev linked, keywords did not", jev_only),
                         ("Keywords linked, Jev did not", kw_only)):
        pairs = [(r, s) for r, s in pairs if s][:15]
        if pairs:
            L += [f"## Spot-check: {title}", "", "| capture | pages | existing | fits |", "|---|---|---|---|"]
            for r, s in pairs:
                fits = ", ".join(f"{n.split(' (')[0]} {v:.2f}" for n, v in r["fits"].items())
                L.append(f"| {r['title'][:70].replace('|', '/')} | {', '.join(sorted(s))} | "
                         f"{', '.join(r['gold'])} | {fits} |")
            L.append("")

    (run_dir / "report.md").write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- mock

def mock_transport():
    """Offline stand-in for the API that exercises the SDK's real request/response path.
    Answers come from simple word overlap and cue words, NOT from Jev: good enough to
    click around the app without a key, meaningless as results."""
    import httpx2

    stop = set("the a an and or of to in on for with is are be by as at it this that from your you my our "
               "notes note concept page how what".split())
    words = lambda s: {w for w in re.findall(r"[a-z0-9]+", str(s).lower()) if len(w) > 2 and w not in stop}
    promo_cues = ("% off", "link in bio", "100x", "sponsored", "book a demo", "free today", "seats left", "not financial advice")

    def handler(request):
        body = json.loads(request.content)
        cap = body["state"].get("capture", {})
        text = f"{cap.get('title', '')} {cap.get('text', '')}".lower()
        tw = words(text)
        answers = {}
        for qid, q in body["questions"].items():
            if q["type"] == "choice":
                opts = list(q["criteria"])
                w = [0.3 if o == NONE else 0.05 + len(tw & words(f"{o} {q['criteria'][o]}")) ** 2 for o in opts]
                w = [x / sum(w) for x in w]
                best = max(range(len(opts)), key=lambda i: w[i])
                answers[qid] = {"type": "choice", "choice": opts[best], "confidence": round(max(w), 3),
                                "probabilities": dict(zip(opts, w))}
            elif q["type"] == "score":
                n = len(q["criteria"])
                depth = min(n - 1, len(tw) // 12) if not any(c in text for c in promo_cues) else 0
                p = [0.1 / (1 + abs(i - depth)) for i in range(n)]; p[depth] += 0.6
                p = [x / sum(p) for x in p]
                answers[qid] = {"type": "score", "score": sum(i * x for i, x in enumerate(p)),
                                "confidence": max(p), "legend": {str(i): c for i, c in enumerate(q["criteria"])},
                                "probabilities": {str(i): x for i, x in enumerate(p)}}
            elif "promot" in q["instructions"]:
                answers[qid] = {"type": "noul", "noul": 0.85 if any(c in text for c in promo_cues) else 0.12}
            else:
                i = int(qid.split("_")[-1])
                c = body["state"]["candidates"][i]
                overlap = len(tw & words(f"{c['name']} {c['description']}"))
                answers[qid] = {"type": "noul", "noul": round(min(0.95, 0.1 + 0.22 * overlap), 3)}
        return httpx2.Response(200, json={"model": "mock", "answers": answers,
                                          "usage": {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": 0}})

    return httpx2.MockTransport(handler)


# --------------------------------------------------------------------------- main

async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--captures", default=str(CAPTURES_DIR))
    ap.add_argument("--catalog", default=str(CATALOG_FILE))
    ap.add_argument("--set", choices=["default", "labeled", "unlabeled", "all"], default="default")
    ap.add_argument("--unlabeled", type=int, default=100, help="unlabeled sample size for --set default")
    ap.add_argument("--limit", type=int, default=0, help="cap the number of captures (0 = no cap)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--model", default="jev-latest")
    ap.add_argument("--fit", type=float, default=0.5, help="stage-2 Noul threshold for a link")
    ap.add_argument("--none-skip", type=float, default=0.8, help="skip stage 2 when p(none) >= this")
    ap.add_argument("--shortlist", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--resume", help="existing run folder to continue")
    ap.add_argument("--mock", action="store_true", help="offline dry run, no API key needed")
    args = ap.parse_args()

    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    by_name = {p["name"]: p for p in catalog}
    names = set(by_name)
    caps = load_captures(Path(args.captures))
    rnd = random.Random(args.seed)
    labeled = [c for c in caps if c["gold"]]
    unlabeled = [c for c in caps if not c["gold"]]
    if args.set == "labeled":
        pick = labeled
    elif args.set == "unlabeled":
        pick = unlabeled
    elif args.set == "all":
        pick = caps
    else:
        pick = labeled + rnd.sample(unlabeled, min(args.unlabeled, len(unlabeled)))
    rnd.shuffle(pick)
    if args.limit:
        pick = pick[: args.limit]

    run_dir = Path(args.resume) if args.resume else HERE / "runs" / time.strftime("%Y-%m-%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    out_jsonl = run_dir / "results.jsonl"
    done = {}
    if out_jsonl.exists():
        for line in out_jsonl.read_text(encoding="utf-8").splitlines():
            r = json.loads(line); done[r["id"]] = r
    todo = [c for c in pick if c["id"] not in done]
    print(f"{len(caps)} captures loaded ({len(labeled)} labeled) · catalog {len(catalog)} pages · "
          f"running {len(todo)} (+{len(done)} resumed) → {run_dir}")

    kw = dict(model=args.model, retry=RetryPolicy(max_retries=4, backoff_max=10.0, timeout=60.0))
    if args.mock:
        kw.update(api_key="mock", transport=mock_transport())
    sem = asyncio.Semaphore(args.concurrency)
    errors = 0
    async with AsyncTypeSafeClient(**kw) as client:
        with out_jsonl.open("a", encoding="utf-8") as fh:
            async def worker(c):
                nonlocal errors
                async with sem:
                    try:
                        r = await triage_one(client, c, catalog, by_name, args)
                    except TypeSafeError as e:
                        errors += 1
                        print(f"  ! {c['id']}: {type(e).__name__}: {e}", file=sys.stderr)
                        if errors == 1 and "Authentication" in type(e).__name__:
                            raise
                        return
                    done[r["id"]] = r
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n"); fh.flush()
                    if len(done) % 25 == 0:
                        print(f"  {len(done)} done")
            await asyncio.gather(*(worker(c) for c in todo))

    rows = [done[c["id"]] for c in pick if c["id"] in done]
    with (run_dir / "results.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "source", "title", "existing_links", "jev_links", "choice", "choice_conf",
                    "p_none", "substance", "promo", "fits"])
        for r in rows:
            w.writerow([r["id"], r["source"], r["title"], "; ".join(r["gold"]),
                        "; ".join(sorted(links_at(r, args.fit))), r["choice"], r["choice_conf"],
                        r["p_none"], r["substance"], r["promo"], json.dumps(r["fits"], ensure_ascii=False)])
    ev = evaluate(rows, names, args.fit)
    (run_dir / "metrics.json").write_text(json.dumps(ev, indent=2), encoding="utf-8")
    write_report(run_dir, rows, ev, args, names)
    print(f"done: {len(rows)} captures, {errors} errors, ${ev['cost_usd']:.3f} → {run_dir / 'report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
