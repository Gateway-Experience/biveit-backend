# The MCP servers this platform hands to its agent sessions -- one folder each, with a main.py that runs
# it (`python -m src.mcps.<name>.main`). They run INSIDE an agent's session, not in the backend, so:
#   - they may import this repo's pure code (constants, utils), which keeps both sides of every contract
#     in one place;
#   - they must never import src.config (or anything that does): loading settings pulls our .env files,
#     secrets included, into a process the agent controls. What they need comes in their own env vars.
