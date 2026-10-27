# The deployments module's services. deployments.py runs the show; the others each own one piece of the
# outside world and know nothing about deployments as a process:
#   sources.py      the workspace's git repo, inside the project's dev container
#   images.py       building a site's image (docker)
#   containers.py   the site's container on the host (docker)
#   environment.py  what the site runs with: its prod database access, its env vars
#   sites.py        the site's domains in the host's nginx, with their certificates
