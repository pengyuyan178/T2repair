# Release contents and provenance

This release contains the current T2Repair algorithm, a standalone single-case launcher, paper figures, and the recorded trajectory used in Figure 8. The publication copy was assembled separately from the research workspace.

## Source code

The eight original algorithm modules are under [code/causalgui/](../code/causalgui/). Ordinary Python comments and module/class/function docstrings were removed. After normalizing docstrings, each published module has the same executable abstract syntax tree as its source. Model prompt strings and embedded JavaScript/Python tool programs were preserved. [source-manifest.json](source-manifest.json) records the original paths and before/after hashes.

The Python package name `causalgui` and some internal field names predate the paper title. They remain where they are part of the active implementation or saved artifact format. There are no archived algorithm versions, baseline implementations, dataset caches, model credentials, or `old_data` directories in the release.

[cli.py](../code/causalgui/cli.py) and [__main__.py](../code/causalgui/__main__.py) are release packaging. They provide base-checkout preparation, container runtime setup, preflight, method execution, and prediction export. They replace the historical launchers' dependencies on a shared experiment controller and baseline adapters. The release launcher does not reproduce the original server's external request broker, batch scheduling, or environment snapshots. Core algorithm preservation does not imply that a new API call or a different dependency environment reproduces a recorded patch exactly.

The copied tests retain the algorithm and browser checks. Their pipeline fixture now checks its stage ledger locally instead of importing a server-specific launcher; its temporary directories are isolated per test. One test of the historical external broker's budget auditor is omitted because that broker is not shipped. New release-command tests cover the preparation/export boundary. These adaptations are recorded in the source manifest.

## Figures and trajectory

The [eight PDFs](../figures/README.md) are byte-for-byte copies of the current manuscript figures. Their original filenames and hashes are in [figures/manifest.json](../figures/manifest.json). No SVG or raster exports of those figures are included.

The [Figure 8 archive](../trajectories/fig8-prism-1853/README.md) contains 451 hash-listed files from the recorded DeepSeek run. Model responses, source excerpts, scripts, timestamps, and original audit paths remain unchanged. The exported official result is the matching row from the original final-results CSV, with that CSV's hash retained. The full CSV and other methods' experiments are outside this release.

Raw issue images and browser screenshots are experimental evidence. Existing README mascot illustrations remain in `assets/`. Both are separate from the PDF-only paper-figure collection.

The verification command checks archived evidence; it does not repeat generation or official evaluation. The saved official pass and the separate `unconfirmed` strict-audit status are both retained.

## Release validation

[release-checks.json](release-checks.json) records the completed checks. All 161 implementation tests passed inside the release Docker image with networking disabled, including real Chromium interactions and replay. The installed package matches the release source hashes. A separate prepared-case smoke check confirmed base-checkout validation, Babel parsing, Node entry loading, and Chromium availability. The eight paper PDFs and all 451 archived trajectory files passed integrity checks.
