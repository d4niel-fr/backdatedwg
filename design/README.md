# Handoff: AutoCAD Version Converter (working name "Backdate.dwg")

## Overview
A 3-screen web app. The user uploads an AutoCAD DWG or DXF file, picks an older target version (e.g. 2026 file -> 2010 file), waits while it converts, then downloads the result. Entities that cannot be converted are skipped without failing the job and are listed in a conversion report.

## About the Design Files
`DWG Converter.dc.html` is a **design reference built in HTML**. It shows intended look and layout; it is not production code. Recreate it in the target codebase's framework and patterns (React, Vue, etc.). If no codebase exists, pick a suitable framework. The file lays all three screens side by side on a canvas (each 880px wide); in the real app they are one screen at a time.

## Fidelity
**High-fidelity.** Colors, type, radii and spacing are final and come from the "Organic" design system (`_ds/organic-.../styles.css`, copied in this folder). Recreate pixel-accurately. All copy, file names, sizes and skipped items are placeholders.

## Screens

Shared: page background `--color-bg` #f5ead8. Each screen is a card: radius 28px (`--radius-lg`), shadow `--shadow-lg`, padding 40px 56px 56px, min-height 640px. Header row: wordmark left (Caprasimo 24px, "Backdate" in accent #c67139, ".dwg" in text #201e1d), 56-64px gap below. Layout is left-aligned and asymmetric.

### 1. Upload
- **Purpose:** choose file and target version.
- Header right: tag "Free · no sign-up" (`.tag-neutral`).
- H1 "Open any AutoCAD file in any version." Caprasimo 56px / 1.05, max-width 620px, margin-bottom 16px.
- Subtext 18px, color neutral-700 #645c50, max-width 520px: "Upload a DWG or DXF and save it as an older release. Problems are skipped and listed in a report."
- Two columns, gap 24px:
  - **Drop zone** (flex 1.4): 2.5px dashed border accent-400 #f6a06b, fill accent-100 #fff2eb, radius 28px, padding 44px 32px, content left-aligned, gap 12px. Contains a 64px circle (accent #c67139) with white upload icon (Lucide `upload`, stroke 2.75, 30px); title "Drop your file here" (Caprasimo 26px); "`.dwg` or `.dxf`, up to 200 MB" (neutral-700); `.btn .btn-primary` "Choose file".
  - **Options card** (flex 1): fill #fffaf0, radius 28px, padding 28px, `--shadow-sm`, gap 18px. "Convert to" (700) with pill chips 2000, 2004, 2007, 2010, 2013, 2018 (selected = `.tag-accent`, others `.tag-outline`); "Format" with chips DWG (selected, `.tag-accent-2`) and DXF.
- Behavior: dropping/choosing a file starts upload and conversion immediately (or enables a "Convert" button; product decision). Default target 2010.

### 2. Converting
- **Purpose:** show progress.
- File row: card #fffaf0, radius 28px, padding 20px 24px, `--shadow-sm`, max-width 560px, margin-bottom 40px. 48px circle (accent-2-200 #e1eecc, text "DWG" 12px/700 color accent-2-800 #3d472b), name (700) "Floorplan_Level3_rev12.dwg", meta 14px neutral-700 "AutoCAD 2026 · 48.2 MB", arrow, tag-accent "2010".
- H2 "Converting to 2010…" Caprasimo 48px / 1.05. Subtext 18px: "Rewriting blocks and layers. This usually takes under a minute."
- Progress bar: max-width 560px, height 20px, pill track accent-200 #ffe1d0, fill accent #c67139. Below: percent left, ETA right, 14px neutral-700.
- Step list (16px, gap 12px, 22px circle markers): done = accent-2 #7a8a5e circle with white check; current = bold, 3px accent spinner ring; pending = muted neutral-500 text, 2px neutral-300 ring. Steps: Reading file structure / Converting N layers and blocks / Writing 2010 file · N items skipped so far / Building report.
- `.btn .btn-ghost` "Cancel" (aborts job, returns to Upload).

### 3. Download
- **Purpose:** get the file and review skipped items.
- Two columns, gap 40px, align top.
  - **Left:** 72px circle accent-2 with white check icon (34px); H2 "Your 2010 file is ready." Caprasimo 52px / 1.05; line "name · 2026 -> 2010 · 31.7 MB" 18px neutral-700; buttons (gap 12px) `.btn-primary` "Download DWG" and `.btn-secondary` "Convert another" (returns to Upload); note 14px "Files are deleted after 1 hour."
  - **Right: report card:** fill #fffaf0, radius 28px, padding 28px, `--shadow-sm`. Title "Conversion report" (Caprasimo 22px) + `.tag-accent` "N skipped". Intro 14px neutral-700. Items stacked gap 12px: rows with fill accent-100, radius 12px, padding 12px 16px; line 1 "**Entity type** · Layer name" 15px; line 2 reason 13px neutral-700. `.btn-ghost` "Download report (.txt)".
- Zero skipped: replace tag with a sage tag "No issues" and hide the list.

## Interactions & Behavior
- Flow: Upload -> Converting -> Download. Cancel returns to Upload. "Convert another" resets.
- Hover/pressed/focus come from the design system (accent-600 hover; 2px accent `:focus-visible` outline, offset 2px). Disabled = 45% opacity.
- Progress bar width animates smoothly (~300ms ease); spinner rotates continuously.
- Errors to design/handle: unsupported file type, file over 200 MB, corrupt file (whole-job failure, distinct from skipped entities), network failure. Not mocked yet; use a `.tag-accent` + message in the drop zone.
- Responsive: mocks are desktop-only (880px cards). Stack columns below ~700px.

## State Management
- `step`: 'upload' | 'converting' | 'done' | 'error'
- `file` (name, size, detected version), `targetVersion` (default 2010), `format` ('DWG'|'DXF')
- `job`: id, progress 0-100, current step index, skippedCount, etaSeconds
- `result`: downloadUrl, outputSize, skipped[] ({entityType, layer, reason}), reportUrl
- Backend: upload file, create job, poll or stream progress, fetch result. The conversion engine (e.g. ODA/Teigha File Converter or Open Design Alliance libraries, or LibreDWG for limited cases) is not part of the design. Auto-delete outputs after 1 hour.

## Design Tokens (from `styles.css`)
- Colors: bg #f5ead8, surface #ebddc5, text #201e1d, accent #c67139, accent-2 #7a8a5e, card fill #fffaf0
- Accent ramp: 100 #fff2eb, 200 #ffe1d0, 300 #ffc6a5, 400 #f6a06b, 500 #d67f48, 600 #b2622d, 700 #8c491a, 800 #643312, 900 #402310
- Accent-2 ramp: 100 #f0fae1, 200 #e1eecc, 300 #ccdbb2, 400 #aebf92, 500 #8fa073, 600 #728157, 700 #56633f, 800 #3d472b, 900 #272e1b
- Neutral ramp: 100 #f9f4ed, 200 #eee7db, 300 #dcd3c4, 400 #c0b6a5, 500 #a19786, 600 #82796a, 700 #645c50, 800 #474238, 900 #2e2b25
- Fonts: headings Caprasimo 400; body Figtree 400/600/700 (Google Fonts)
- Radius: sm 8, md 16, lg 28, buttons/chips/inputs 999px
- Spacing: 4.4 / 8.8 / 13.2 / 17.6 / 26.4 / 35.2px
- Shadows: sm `0 1px 2px #2e2b25@14%`, md `0 3px 10px @16%`, lg `0 12px 32px @22%`
- Icons: Lucide, stroke-width 2.75

## Assets
No images. Icons are inline Lucide-style SVGs (upload, check). Wordmark is text.

## Files
- `DWG Converter.dc.html` - the design (all 3 screens)
- `_ds/organic-f8045522-d100-4084-a0e3-d8868ace2846/styles.css` and `readme.md` - design system tokens, `.btn`, `.tag` classes and guidance
