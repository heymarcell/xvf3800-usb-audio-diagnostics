# Published diagnostic results

Do not commit the entire raw `runs/` tree by default.

For each run intended to support issue #39, publish the compact evidence set under:

`results/issue-39/<run-id>/`

Validation runs are published with:

```bash
python tools/publish_validation.py validation/<timestamp> --with-matrix
```

Single diagnostic runs are imported with:

```bash
python tools/import_run.py runs/<run-id>
```

The importer copies the report, JSON/CSV summaries, GitHub comment draft and compact share bundle, then writes SHA-256 hashes for every published artifact.

If an artifact exceeds GitHub's normal file-size limits, publish it as a GitHub Release asset and put its checksum/link in the result README instead of forcing it into Git history.
