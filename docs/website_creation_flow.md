# When a project is created:

Insert new record in `projects` table.
- Each project has a id (a number which you can see on the URL) and a project hash (which looks like a random string unique to that project).

Create the project dir at: `<STORAGE_DIR>/<user hash>/<project hash>/`. The agent actually works one level down, in `<STORAGE_DIR>/<user hash>/<project hash>/<conversation id>/` (where the code, spec docs and uploads go).
- STORAGE_DIR is currently `/home/ubuntu/data/storage_root/` on the VPS.
Copy content of data/template into that conversation folder (for now there is only the CLAUDE.md **website creation process instruction** file)
Create a docker container for the project (which will be used for running the claude session, and for running the project's developmental FE and BE servers)
- The docker container will have a limit on ram (3GB), cpu (1 core), number of processes and open files (to prevent it from hogging the VPS's resources)
- The name of the docker container is derived from the project id (because we will have many projects running at the same time, so we need a way to tell the containers apart)
- The container will claim ports 8000+id and 16000+id for its BE and FE dev servers. So project id 1 will claim port 8001 & 16001, project id 3 claims 8003 and 16003 etc.
- We will mount the project dir (as `/project`) and a small socket folder (as `/sockets`, the BE talks to the runner through it) into the container so they can access those folders (and only them, docker containers cant access any other host directory by default)
- Docker containers can't talk to each other. They can reach out to the host (only on ports 3002 to access the BE's agent endpoints, including the model proxy, and 27017 to access Mongo for the project's database)
- Copy and install the runner (which include claude sdk, skills, mcps) into the container, so the claude session can run in the container. It's read-only for the agent (it can't modify its own runner), and it's version-checked every time it starts, so runner updates reach every project automatically.
Start the runner inside the container (a python file). This file basically starts a claude session (in sdk mode), and then acts as a middleman between the sdk and the BE
- If the runner restarts (crash, idle shutdown, server reboot), it continues the same claude session from its transcript, saved in the project dir (`.devenv/home/.claude/...`), so the agent doesn't forget the conversation.
- Send the session a first message with the project's info (project name, description, the starting form's answers, user's language), telling it to start planning and send the first message to the user. This only happens once per conversation.

With this, a project (code, session etc) is contained and are safe from touching the main BE server or other projects.

# Folder structure

STORAGE_DIR (currently /home/ubuntu/data/storage_root): the root directory to store all persistent files of the platform
  <user1 hash>: we group every file under the user's control here.
    <project1 hash>: we group every file under the project here. This is where the docker container **mounts**
      .devenv/home: acts as the home folder of the docker container. So claude session transcripts will live here (under .devenv/home/.claude)
      <conversation id>: we group every file under the conversation here. Currently each project only has 1 conversation.
        CLAUDE.md: main instruction file
        artifacts: store artifacts generated for the project. E.g. erd.md
        uploads: store uploaded files
        frontend: store frontend code of the project, first copied over from biveit-boilerplate
        backend: store backend code of the project, first copied over from biveit-boilerplate
    <project2 hash>: we group every file under the project here
  <user2 hash>: ...

docker container fs are ephemeral, they go away when the docker is recreated/destroyed. So we mount the host's project folder of the project into the container, everything written there will be persisted.

Imagine the docker container as a PC without a hard disk, it's only running ubuntu through a live CD. Nothing gets persisted after the PC is reset. Mounting a host folder into the container is like plugging a USB flash into that PC. Whatever it writes into the USB flash stays.

# Conversation mechanics (how the session talks to the user and vice versa)

User sending messages:
User -> Browser (FE) -> BE -> Runner (python script, which uses claude sdk) -> claude session -> BE proxy -> external service (Anthropic/deepseek)

AI sending messages:
external service (Anthropic/deepseek) -> BE proxy -> claude session -> Runner (claude sdk) -> BE -> Browser (FE) -> User

When BE wants to send message to the session (e.g. sending user's messages), it requests to the runner and the runner use sdk api to talks to the session.
The session then sends requests to Anthorpic or deepseek, but through a proxy we set up in the BE. We dont want the session to send requests directly to 3rd-party services, because to do so they would have access to the api key/token. An user could ask the session for that api key. With the proxy, the api key lives there instead of inside the container so we're safe. (The session gets a token instead, which only works through our proxy and only for its own conversation.)

Why we need the runner: because we need to something to run claude sdk (which is a python library). Why not run claude sdk inside BE? Because we want claude sdk to run inside the container, for security reasons. By the way, we only have 1 BE process, but 1 runner for each container. These all have 1-1 relationship: runner <-> container <-> project <-> claude session.

When external services sends back to claude session, there are many messages the sent (e.g. "Let me check how the feature works", or "Let me research online"). An agent's turn might have many messages then. But we only send the final message in a turn back to the users (and discard the rest), so the users aren't overwhelmed with messages.

Sometimes the session also uses the communication MCP (we wrote it) to send special signals, like "Website is being built", or "Website building is done"

# When a project goes into Building stage:

Building starts once user clicks Approve to approve the spec. Then the project record in db is updated so we know it has entered the Building stage at least once.
- To be clear this is the postgres db (the BE db, not to be confused with mongodb which the projects use).

Copy the boilerplate frontend and backend into the project's folder (which had been mounted above).

Create a dedicated mongodb user (if not exist already) for the user. FYI the mongodb service lives in another docker container. Each user has his own mongodb username + password and that db user only has access to his own dbs, so he can't access other users' dbs in mongodb.

Initialize .env files for this project (generate randomized secrets, derive certain values from project id like ports etc).

Install dependencies (npm install, uv sync etc).

Start the FE and BE servers inside the container.

Generate a domain name for the preview site under `<project hash>.<DEV_DOMAIN>` (e.g. `<project hash>.proj.biveit.com`), and configure nginx to make it public. The preview site should now be ready.

Start building (writing code) for the site. The project's FE and BE dev servers should hot-reload as the code changes.

Things that would consume resources (RAM etc.) here:
- The container itself
- FE and BE dev servers
- The runner and claude session
That's why the admin panel has a **stop project** action. Also projects idle for 2 hours would be stopped. Stopping means stopping the dev servers, the claude session, and the container. When someone visits the project on the FE again, the system automatically starts up those 3 things for the project again.
