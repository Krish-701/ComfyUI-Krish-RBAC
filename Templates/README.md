# Shared workflow templates

Place workflow `.json` files in this folder:

```text
ComfyUI/custom_nodes/Templates/
```

(or keep a copy next to this extension under `custom_nodes/Templates`).

## Behaviour

- **All users** (user, power, admin, guest) can **see and open** these templates.
- Templates are **read-only** in the UI — users cannot save over or delete them.
- To use a template: open it, then **Save As** into the user’s own workflows folder.

## Admin

- Drop new `.json` workflow files into this folder on the server (filesystem).
- Restart is not required for new files to appear on the next list refresh.
