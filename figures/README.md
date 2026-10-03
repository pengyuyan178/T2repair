# Paper figures

All eight paper figures are provided as the original PDFs. Figure numbers follow their order in the manuscript.

| Figure | Content | PDF |
| --- | --- | --- |
| 1 | Visual bug scenarios | [fig1-visual-scenarios.pdf](fig1-visual-scenarios.pdf) |
| 2 | Motivating example | [fig2-motivating-example.pdf](fig2-motivating-example.pdf) |
| 3 | T2Repair framework | [fig3-framework.pdf](fig3-framework.pdf) |
| 4 | Code Agent investigation | [fig4-code-investigation.pdf](fig4-code-investigation.pdf) |
| 5 | Browser Agent investigation | [fig5-browser-investigation.pdf](fig5-browser-investigation.pdf) |
| 6 | Repair overlap | [fig6-repair-overlap.pdf](fig6-repair-overlap.pdf) |
| 7 | Agent ablation overlap | [fig7-ablation-overlap.pdf](fig7-ablation-overlap.pdf) |
| 8 | Repair trajectory on `PrismJS__prism-1853` | [fig8-repair-trajectory.pdf](fig8-repair-trajectory.pdf) |

[manifest.json](manifest.json) records the original manuscript filenames and SHA-256 hashes. Figure 8 also has a [trace walkthrough and raw records](../trajectories/fig8-prism-1853/README.md).

The README displays [PNG previews](../assets/paper/) rendered directly from these PDFs, with each preview linking back to its original PDF. This directory keeps the original PDF figure collection. Mascot illustrations and archived screenshots are separate assets.

To refresh the display previews after updating the PDFs and this directory's manifest:

```bash
python -m pip install PyMuPDF
python scripts/render_readme_figures.py
```

Run these commands from the repository root. Previews use a 2400-pixel target width, white backgrounds, and the complete PDF page. [assets/paper/manifest.json](../assets/paper/manifest.json) records each source PDF hash, preview hash, and image dimensions. PyMuPDF is only needed to regenerate the documentation previews.
