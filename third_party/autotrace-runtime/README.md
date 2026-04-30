# AutoTrace runtime

This directory contains the Windows runtime used by module 3 and by the PyInstaller build.

- `bin/autotrace.exe` is built from the AutoTrace source tree in this workspace.
- `bin/*.dll` are the runtime DLLs required by that executable.
- `licenses/` keeps the AutoTrace license files copied from the source tree.

The full source and build environments stay ignored under `third_party/`; only this minimal runtime is versioned and packaged.
