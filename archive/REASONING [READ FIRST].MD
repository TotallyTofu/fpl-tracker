Documentation Review — FPL Tracker Workspace



Overall assessment. 



The workspace contains 8 project-authored markdown files (\~3,486 lines). The project shipped v1.0.0 + v1.0.1 on 2026-09-20/21 (verified in git log: M1→M4 commits, v1.0.0 tag commit, retrospective), so most of the planning-era documents have served their purpose. However, not all plan documents are dead: PLAN.MD is still actively maintained (its working tree contains a post-release v1.0.1 Reddit-OAuth update, verified 2026-09-21) and is referenced by section number from the README's season-rollover checklist, which you'll follow every August. PLAN-5-M4-POLISH.MD holds the only spec for a deliberately deferred feature (the entry-rank card, T4.4), which ACCEPTANCE.md explicitly says is "preserved in PLAN-5-M4-POLISH.MD for a future milestone." Everything else — OUTLINE.MD, PLAN-2, PLAN-3, PLAN-4, and the one-shot ACCEPTANCE.md — is historical record with no load-bearing references. 



Verdict: keep 3, mark 5 as \[OBSOLETE] (archiving them removes \~1,837 lines ≈ 53% of the documentation token cost).

Per-file determination



|**File**           |**Lines**  |**Verdict**    |**Why**    |
|               -   |-          |-              |       -   |
|README.md          |506        |KEEP           |The living ops doc (quick start, usage, config, ToS, troubleshooting, rollover checklist). Actively maintained in the working tree.|
|PLAN.MD            |884        |KEEP           |See below — it's now the project's reference manual, not just a plan.|
|PLAN-5-M4-POLISH.MD|259        |KEEP           |T4.4 (entry-rank card) is the spec for the one feature deferred by user decision; ACCEPTANCE.md points here for it. Small, and keeps the option alive.|
|OUTLINE.MD         |558        |**\[OBSOLETE]**|Superseded by PLAN.MD, which declares "where this plan's verified facts contradict OUTLINE.MD, the verified fact wins." Contains a wrong 38-GW deadline table (flagged in V2), the dropped M3 real-time scope, open questions all since resolved (V1–V11, T4.8), and a reference to potential websites for FPL tracking.md — which is already deleted from the tree (git status D). Its unique content (original request text, decisions D1–D8) is duplicated in PLAN.MD §1–§2.|
|PLAN-2-M1-CORE.MD  |449        |\[OBSOLETE]    |Task-level build spec for M1 — fully implemented, accepted, and superseded by the code + 153-test suite.|
|PLAN-3-M2-NEWS.MD  |386        |\[OBSOLETE]    |Same for M2.|
|PLAN-4-M3-REALTIME.MD|227      |\[OBSOLETE] (strongest case)|Describes M3 scope that was explicitly dropped (git commit 098033e, README milestone table: "live pollers + SSE + live dashboard dropped per project decision"). This work was never built; the doc has zero operational value.|
|ACCEPTANCE.md|217|KEEP|One-time v1.0.0 acceptance record + retrospective. The README inlines the full 9-item rollover checklist, so the only thing ACCEPTANCE.md adds is dated evidence (what passed on 2026-09-20) — historical, not operational. Keep only if you want the audit trail close at hand.|



Why PLAN.MD stays (answering your specific question)



You framed it as "the plan I had agents build from" — and it started that way. But the README's "New season (August rollover checklist)" section, which is the one part of the docs you'll actually act on every year, points into it by section number: rules → PLAN.MD §5, scoring table → §5.9, BPS table → §5.10, season-rollover logic → §7, design rationale → §9. It's also the only self-contained copy of the 2026/27 rules engine, the full SQLite DDL (§7), the REST API spec (§8), and the config.json defaults (§10) — none of which the README reproduces in full. And it's demonstrably still maintained (the uncommitted v1.0.1 OAuth row in §4.1). My inference (not a verified fact): future agent sessions fixing bugs or adding features will find §5/§7/§8/§10 valuable. If you want to trim it, the historical front matter (§1 mission, §3 builder operating rules, §12–13 milestone machinery) is the part that's dead weight — but I'd leave it in place, since the README references the file by section number and renumbering would break those links.

