# Reference plugin: `acme-csv`

The smallest complete docpipe extension, and the template to copy.

```bash
docpipe plugin lint examples/plugin-csv
docpipe run --in /in --out /out --plugin /path/to/examples/plugin-csv --offline
```

It adds:

- a **format** (`csv`) that matches on extension *and* on a content sniff, so a
  `.txt` full of comma-separated rows is still recognised;
- an **operator** (`csv_rows`) that emits parsed cells as a table block, which is
  strictly more accurate than any vision extraction on the same file;
- an honest `seekable = false`, because a CSV cannot be sharded without reading
  what came before it. The runtime uses that to avoid partitioning it.

Read `docs/plugin-spec.md` alongside this directory.
