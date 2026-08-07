# Third-party code in this directory

Uplink's own code is MIT (see `../../LICENSE`). One third-party library is
vendored here rather than loaded from a CDN, because a CDN request would put
a network call into a product whose guarantee is that nothing leaves the
machine — and would break offline use.

## gsap.min.js — GSAP 3.13.0

- Source: https://gsap.com (https://unpkg.com/gsap@3.13.0/dist/gsap.min.js)
- Copyright 2025, GreenSock. All rights reserved.
- Licensed under the GreenSock Standard License:
  https://gsap.com/standard-license
- Used for interface motion only. It never touches document content,
  retrieval, or the index.

The file is committed verbatim, license header included. To update it,
replace the file with a fresh download and note the new version here.
