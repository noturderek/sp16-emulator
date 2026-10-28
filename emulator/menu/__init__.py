"""Menu-navigation recipes for the SP-16 emulator (firmware 1.60), driven through a live research session.

Each recipe module is a script of `hwsuite.run_recipe` steps plus a `main()` CLI; see `sync_source.py`.

Note: these live in `emulator/menu/` (not `emulator/recipes/`) because `emulator/recipes.py` already exists as the
profile-recipe validation module; a `recipes/` package would shadow it.
"""
