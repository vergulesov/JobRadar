# JobRadar

Fast vacancy collector for building a **high-recall pool** before AI ranking.

Goal: collect fresh vacancies quickly, deduplicate them, and apply only a conservative hard filter so potentially relevant vacancies are not lost before AI review.

Initial pipeline:

```text
HH RSS -> normalize -> deduplicate -> conservative hard filter -> target pool -> AI (later)
```

Principles:
- no vacancy HTML downloads in the fast collection stage;
- freshness window is configurable;
- deduplicate by vacancy id / canonical URL;
- hard filter removes only obvious noise;
- do **not** cap the target pool to an arbitrary number;
- target pool size is whatever survives the conservative filter;
- AI ranking is a separate later stage.

## Current target profile

Broadly keep vacancies related to:
- B2B sales;
- project / technical sales;
- account management / key accounts;
- industrial, engineering, IT and other business products/services;
- incoming requests, specifications, calculations, commercial proposals, tenders, contracts, supply and project coordination.

Hard-reject only clearly irrelevant categories or clearly incompatible work formats. Ambiguous vacancies should survive into the target pool for later AI/human review.
