# Slicer profiles

The fab service slices with **PrusaSlicer CLI** using an exported config INI.
A real profile has hundreds of keys tuned to the printer/filament — it is NOT
shipped here (a wrong profile ruins prints). Export your own:

1. In **PrusaSlicer** (or **OrcaSlicer**), select the Ender-3 V3 KE printer,
   a PLA filament, and a 0.2mm quality preset that you've already printed with.
2. **File → Export → Export Config** → save as `ender3_pla_0.2mm.ini` here.
3. Confirm `slicer.config_ini` in `fab-config.json` points at it.

Sanity-check the CLI before wiring it in:

```bash
prusa-slicer --export-gcode --load profiles/ender3_pla_0.2mm.ini \
  --output /tmp/test.gcode /path/to/a-known.stl
grep -E "estimated printing time|filament used" /tmp/test.gcode
```

If those comment lines appear, `fab.fabricate.parse_slice_summary()` will read
time/filament/layers from them.
