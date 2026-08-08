# Uplink: document answers you can check

**A private, self-hosted system that answers questions from an organization's
own documents — and refuses to publish any answer it cannot prove came from
them.**

Built and directed by **Erick Vanderpool** ·
[engine source](https://github.com/evanderpool/uplink) ·
[demo source](https://github.com/evanderpool/uplink-demo) ·
[**live demo →**](https://uplink-demo.onrender.com)

| 2 days | 343 | 95% | ~2¢ |
|---|---|---|---|
| first commit → live public demo | automated tests, all passing | first-answer accuracy, measured (19-question set) | cost per answered question |

---

## The problem

Every organization sits on documents that hold real answers — filings,
manuals, policies, contracts. Getting those answers out has two failure modes.
General-purpose chatbots answer fluently from nowhere in particular: when the
answer matters, you can't tell whether it came from your documents or from thin
air. And most document-AI services solve that by making you upload everything
to someone else's servers — a non-starter for anything confidential.

Uplink is built on a different premise: **the documents stay where they are,
and every answer must prove where it came from.**

## The product

Uplink indexes a folder of mixed documents — Markdown, PDF, Word, modern and
legacy Excel, CSV — into a single searchable database on the machine that owns
them. The workspace is three panels: your **sources** on the left (searchable,
filterable by type, each one selectable in or out of scope), the
**conversation** in the middle, and a **studio** of live quality metrics on the
right. Two ways to get answers:

- **Instant keyword search** across every document, with the matching passage
  highlighted and one click to the original file.
- **Asked-and-answered questions** — typed or dictated by voice — answered
  *only* from retrieved passages, with citations that open the exact spot in
  the exact document.

This public demo runs the engine against ten years of Apple's annual reports —
public SEC filings, in their original Excel format. Ask about a decade of
revenue; click any citation and land on the actual row of the actual filing.

## How every answer is made

"The answer comes from your documents" is a promise most systems make and none
enforce. Uplink enforces it as a pipeline — each step is code, not a polite
request to a model:

```
  Question arrives  (typed or spoken; untrusted data; carries the user's
        │            document selection if they scoped it)
        ▼
  Queued           (the web app has NO AI in it — questions land in a queue
        │            as plain files: a seam that lets any brain answer)
        ▼
  Retrieve         (ranked BM25 search over SQLite full-text index; best
        │            passages, spread across documents, within the user's
        │            selection, full text with source coordinates)
        ▼
  Compose          (the model sees only numbered passages + the question;
        │            answers as a lead sentence + short bullets; cites by
        │            picking passage NUMBERS — coordinates are copied from
        │            the index, never written by the model)
        ▼
  ╔═════════════════════════════════════════════════════════╗
  ║  VERIFICATION GATE                                       ║
  ║  every citation checked against the index:               ║
  ║  does this document exist here, and was it in scope?     ║
  ║    ✓ all check out  → published                          ║
  ║    ✗ any fail       → answer refused, not shown          ║
  ╚═════════════════════════════════════════════════════════╝
        │
        ▼
  Published, inspectable  (citations render as buttons; each opens the exact
                           passage so the reader can check the claim)
```

> **Grounding here is a property of the pipeline, not a request in a prompt.**

And when retrieval finds only part of an answer, the system says exactly that:
it bullets what the documents do contain and names what's missing. Built to
prefer an honest gap over a confident guess.

## Measured, not claimed

Accuracy claims in this field are usually adjectives. Uplink's are numbers with
sample sizes, produced by a repeatable evaluation harness that ships in the
repository:

- **Golden question sets** — curated questions with known correct source
  documents — are scored automatically: was the first result correct (hit@1),
  was a correct one in the top five (hit@5), and how high did it rank (MRR).
- **Every published rate carries its sample size and a 95% confidence
  interval.** A 90% score over ten questions is presented as exactly that — a
  small sample with a wide interval — because overclaiming is the failure the
  product exists to prevent.
- **Every run is logged, and drift is watched.** The evaluation history is a
  file in the repo; the in-app metrics panel re-scores the *live* index against
  the published baseline, so a change that quietly degrades retrieval shows up
  as drift — with the failing questions named, turning a score into a to-do
  list.
- **The panel never invents a number.** Where nothing has been measured it says
  "not measured." Answers the system can't verify are labeled unverified on
  screen.

| Milestone | Set | First answer correct | Ranking quality (MRR) |
|---|---|---|---|
| First baseline (day 1) | 18 questions | **67%** (12/18) | **0.769** |
| After ranking + corpus work (day 2) | 19 questions | **95%** (18/19) | **0.947** |
| Independent corpus (Apple, day 2) | 10 questions | **90%** (9/10) | **0.900** |

That 67% → 95% movement is the point of the harness: improvements were made
*against a measurement*, run by run, not by feel.

## From local to live

The demo didn't start public. It earned its way out, in deliberate stages —
and the interesting part is *who* answered the questions at each stage.

**Stage 1 — private, human-supervised.** The product ran on a private machine,
over private documents, with all writes locked to that machine. Its answering
"brain" was a supervised AI session with a human at the keyboard: a small
**watcher** program monitored the question queue and summoned the session
whenever a question arrived. That session worked under a written contract
([AGENT.md](https://github.com/evanderpool/uplink/blob/main/AGENT.md) in the
repo): retrieve first, compose only from retrieved passages, honour the user's
document selection, publish only through the verification gate.

This phase is what made the verification pipeline trustworthy. Every feature —
scoped questions, citations, the metrics panel, spreadsheet support — was
exercised end to end with a person watching each answer come out, on real
documents, before any autonomous system was allowed to touch the queue. The
supervised sessions kept finding real issues, which is exactly what they were
for: among them a scoping bug where a select-all control quietly ignored a
deselected document — found, fixed, and pinned with a regression test the same
day.

**Stage 2 — the brain swap.** Because the web app itself has no AI in it —
questions go to a queue, and *something* answers through the verification gate
— going public didn't mean rebuilding. It meant plugging a different brain into
the same seam: an API model answering autonomously, under the same contract,
through the same gate. The product code didn't change.

**Stage 3 — public, hardened.** The public deployment is a separate,
inspectable fork with a different trust posture: public documents only, exactly
one write allowed from the internet (asking a question), a five-question limit
per visitor, and a hard monthly spending cap upstream. The launch itself was
gated behind a security review — the address wasn't shared until the posture
passed inspection.

### One engine · two trust postures

| Private edition — *your machine* | Public demo — *this site* |
|---|---|
| Your documents — nothing ever leaves the machine | Public records only (ten Apple 10-K filings) |
| All writes: upload, feedback, notes, tuning | One write: asking — 5 questions per visitor, 3-day reset |
| Brain: supervised AI session, human at the keyboard, summoned by the watcher | Brain: autonomous API worker, ~1¢ per answer, spend-capped |
| No API cost at all | Launch gated by a security review |

*Same engine · same retrieval · same verification gate — only the trust posture
changes.*

## Attention to detail, by example

A system like this is really a pile of small decisions. A few that show the
standard the whole build was held to:

- **A real-world file format, handled the same morning.** A batch of SEC
  filings arrived in legacy 1990s Excel format — unsupported. Instead of asking
  the user to convert files, native support was built and tested within hours;
  the test suite builds a valid legacy-format file byte-by-byte so the tests
  depend on no extra software. Those same files are the documents this demo
  answers from.
- **Error messages that name the actual problem.** Voice input's first version
  said "could not hear that" for four different failures. Now a blocked
  microphone, a wrong input device, an unreachable speech service, and plain
  silence each get their own message with its own fix — because one vague error
  hides four different solutions.
- **Metrics a client can read.** The quality panel says "first answer correct"
  and "ranking quality," not hit@1 and MRR — the technical terms live one layer
  down, in tooltips that also carry the exact command that reproduces the
  number. Readable for a recruiter, checkable for an engineer.
- **Tuned against real questions after going live.** The first live test of a
  decade-spanning question exposed that ranked search fed the model passages
  from a single filing (each annual report only covers three years). Retrieval
  now deliberately spreads its reading across multiple documents — and when a
  passage was found arriving cut off just before the requested line item, the
  pipeline was fixed to hand the model full passages. Each fix was verified
  against the live site and pinned with tests.
- **Even the limits are polite.** A visitor who uses their five questions is
  told exactly when they come back, and that keyword search stays unlimited.
  Deselected documents stay readable on screen instead of fading out. The
  interface never punishes curiosity.

## How it was built

This project is also a demonstration of a working method. I directed the build;
AI coding sessions executed it. The division of labor was strict, and it's the
reason two days of pace didn't cost quality:

- **I owned the decisions** — the privacy model, what gets verified and how, the
  scope of every phase, what ships publicly versus stays private, cost ceilings,
  and the security gates. Each meaningful decision is written down in an
  append-only decision log.
- **AI sessions owned the keystrokes** — implementation, test-writing,
  refactoring — phase by phase against the scope I set, with the
  supervised-testing loop above proving each phase on real documents before the
  next began.
- **Nothing merged on trust.** Every phase landed with automated tests (25 on
  day one, 343 today), and dedicated adversarial review passes were run against
  each surface — sessions instructed to attack the work. Those reviews produced
  over thirty findings, including one critical; every one was fixed and then
  pinned as a regression test so it cannot quietly return.

| Day | Shipped |
|---|---|
| **Day 1** | Core engine: multi-format indexing, ranked search, evaluation harness, first baseline (67%). Then document collections, the local web workspace, the question queue with the supervised brain, and citation verification. |
| **Day 2** | Metrics panel with confidence intervals and drift detection; grounding made mechanically enforced; legacy Excel support driven by a real batch of filings; voice input; source search and filtering; and this public demo — deployed, rate-limited, tuned against live questions, and answering on its own. |

## Engineering choices worth noticing

- **Radically small dependency surface.** The core engine runs on Python's
  standard library and SQLite — no web framework, no vector database, no
  orchestration stack. Deployment is "copy the folder." Format readers are
  optional add-ons that degrade gracefully.
- **The brain seam.** Keeping the AI out of the product and behind a queue meant
  the supervised brain and the autonomous brain were interchangeable — the swap
  that took this demo live changed no product code, and the same seam is how a
  future client deployment chooses its own trust level.
- **Cost designed to a ceiling, not a hope.** Free hosting; an answering model
  that costs a couple of cents per question; per-visitor limits; a hard monthly
  spend cap upstream. The worst possible outcome is "the demo pauses" — never a
  surprise bill.
- **Honesty as an interface principle.** Unmeasured numbers say "not measured."
  Unverified answers are labeled. Partial coverage is answered partially with
  the gap named. The product's credibility budget is spent nowhere.

## Check everything on this page

This document follows the product's own rule: no claim without a source you can
inspect.

- **The accuracy numbers** are in
  [the logged evaluation history](fixtures/eval-history.jsonl), reproducible
  with the repo's eval command.
- **The tests** run with one command (`python -m pytest`) in
  [this repository](https://github.com/evanderpool/uplink-demo).
- **The verification pipeline** is readable code — citation checking in
  [`uplink/asks.py`](uplink/asks.py), the demo's answering worker in
  [`uplink/api_brain.py`](uplink/api_brain.py), the visitor limit in
  [`uplink/ratelimit.py`](uplink/ratelimit.py).
- **The supervised-brain contract** is
  [AGENT.md](https://github.com/evanderpool/uplink/blob/main/AGENT.md) in the
  engine repo — the written rules every brain answers under.
- **The two-day timeline** is the public commit history of
  [the engine repo](https://github.com/evanderpool/uplink).

---

**Erick Vanderpool** — data analyst and AI engineer. Uplink is a portfolio
project of Artificial Management.
[github.com/evanderpool](https://github.com/evanderpool) ·
erick.vanderpool2@outlook.com
