# VNTextPatch — vendored upstream

This folder is a verbatim copy of **VNTextPatch** from
[arcusmaximus/VNTranslationTools](https://github.com/arcusmaximus/VNTranslationTools),
release **v0.0.41**, licensed **MIT**.

It is used here to extract/insert text between the System-NNN `.spt` scripts and our
editable JSON corpus (officially supported by the tool). `VNTextPatch.exe.config` carries
the `ProportionalWordWrapper` settings that `libraries/fontcfg.py` keeps in sync with the
render font — do not hand-edit those when a custom font is configured.

Re-vendor with the release asset `VNTranslationTools.zip` (the `VNTextPatch/` subfolder).
