# Release contents and provenance

This repository contains the T2Repair implementation, a standalone single-case launcher, paper figures, and the recorded trajectory used in Figure 8.

## Source code

The implementation is under [code/causalgui/](../code/causalgui/). [source-manifest.json](source-manifest.json) records the source paths and hashes of the published modules.

The internal Python package name is `causalgui`. Model prompts and tool programs are included with the implementation.

[cli.py](../code/causalgui/cli.py) and [__main__.py](../code/causalgui/__main__.py) provide base-checkout preparation, runtime preflight, method execution, and prediction export. See [Run T2Repair](RUNNING.md) for the Docker workflow, model configuration, and evaluation instructions. The launcher runs one case at a time; reproducing a recorded patch also depends on the model endpoint and target project's dependency environment.

## Figures and trajectory

The [paper figures](../figures/README.md) are available as PDFs, with filenames and hashes in [figures/manifest.json](../figures/manifest.json). The README embeds [previews](../assets/paper/) rendered from those PDFs. Clicking a preview opens the corresponding PDF.

The [Figure 8 archive](../trajectories/fig8-prism-1853/README.md) contains the recorded DeepSeek run: model requests and responses, source excerpts, tool observations, candidate patches, validation, and frozen replay. Its manifest records relative paths and file hashes. The saved official result is the matching row from the experiment's final-results CSV, with that CSV's hash retained.

Machine-specific host roots in archived paths use `<your_path>` placeholders. Archive file and payload hashes refer to these anonymized records.

Raw issue images and browser screenshots are included in the trajectory archive. README mascot illustrations are in `assets/`.
