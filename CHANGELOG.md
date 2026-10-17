# Changelog

The AFC server's releases, newest first. AFC has ONE version number for the whole product, this
server and the AFC_Frontend website together: a release is tagged `vX.Y.Z` in every repo it changed
(this repo on its merge commit to main), and the number is counted across both (`release.json`).

Rule R89 (owner, 17 Oct 2026). Write the bullets in the PR, under `## Unreleased`, in plain words
about what a person or a partner notices. The headings decide the kind of release:

- `### Breaking` makes it a **major** (v7.4.2 to v8.0.0): something stops working for people or
  systems outside. A partner API field or address removed or renamed is Breaking (Game Evo reads
  it); so is anything that makes every user sign in again. Only on the owner's word.
- `### Added` or `### Changed` makes it a **minor** (v7.4.2 to v7.5.0): a new feature, endpoint or
  API field (additive), or a deliberate change to how a feature behaves.
- `### Fixed`, `### Security` or `### Internal` alone makes it a **patch** (v7.4.2 to v7.4.3).

At release time `node ~/.claude/skills/versioning/scripts/release.mjs next` prints the number;
rename `## Unreleased` to it, merge, then tag the merge commit. A version tag pushed with the wrong
number is blocked before the push.

## Unreleased

### Internal
- Releases follow rule R89: this changelog and `release.json` (one number with AFC_Frontend).

## v7.1.127 - 2026-10-17

The release this server's tags start from: email news (AFC-B #132, merge 29e4bc4f) shipped with the
website's v7.1.127. The server had no tags before; its history is the merged pull requests.
