# Current state

Deployed to the same 4gb server, dev mode (no docker container, no built frontend dist).

Landing page built but not deployed.

## Agent flow

There is a chat interface in the FE to let users talk to the agent in the BE. the flow is generally like this:

User <-> chat interface (browser) <-> BE (api server) <-> communication MCP <-> claude code sessions.

The communication MCP is worth describing. It uses the experimental event-channel feature of claude code, allowing an external entity to send a message to claude code (before claude code would have to reach out itself if it wants new information). Anthorpic released this feature as the answer to OpenClaw. This MCP though is our own creation. It does 2 things:
- Receive events/messages from the backend (which are usually users' messages or answers sent from the FE). The BE relays them to the MCP, which in turn relay them to the claude session.
- Let the claude sessions send messages back to the BE which in turn sends this back to the users.
In short the MCP acts as the middleman between the BE server and the claude sessions.
Can this MCP be cut and let BE and claude sessions talk to each other directly? In all honesty, it could probably be done but that would require more hacking and the we'd lose a lot of flexibility. Using a standard tech like MCP makes things much easier.
(note that the event channel feature needs a special flag when turning on a claude session because it's still in experimental phase, which we do provide)

The MCP also makes it easy to develop new "commands" or special messages that the claude sessions and the BE can send to each other. For example, the claude session could send a "init done" so the FE would know to stop loading the first time users create a project. The MCP also defines the message formats of multiple choice questions or any future message formats.

*Techincal*: the MCP uses stdio transportation method (instead of http) and is started as a process for each claude sessions (so the number of mcp processes are equal to the number of claude sessions). It also starts a lightweight webserver so it could listen to the BE's requests (see TCP Ports below).

The claude sessions are started in auto mode so it can do work without need a human supervisor. It has a system prompt in the form of a CLAUDE.md file (let's call it *instruction prompt*) to instruct it how to help users build websites. For now it also has a playwright mcp to do testing and skill ui skill to help it build better UI. It's currently running a $100 subscription package. And of course it runs in the same vps server as the whole platform.
- It's managed by tmux to help getting pass startup actions like "do you trust this folder" etc.

### The flow

When a project is first started they go through the following *stages*:
- Bootstrap form: providing name, description and some basic information. The project is not created at this stage, until "Submit" button is clicked.
- Spec-ing: after creating the project, the development env and the claude session is initialized and the first message is sent to the user. The user proceeds to answer agent's questions and discussing the spec of the webiste. This has 3 substages:
  - Gathering business requirements: just general domain and business requirements and the overall website functionality.
  - Confirming ERD: flesh out important entities and their relationships.
  - Confirming the flows: flesh out sequential user flows.
  - Confirming the web pages: individual pages (dashboard, listing page, details page etc) that the website will have.
- Implementing: once the user approves everything in the spec-ing stage, the agent starts implementing (writing code), turns on the project servers and show the user the preview after it's done. They can then iterate on the website as much as they want.
- Deployment: after the user is happy with the website, we should them the payment modal, which marks the transition to the deployment stage. Nothing is being done in this stage yet. Also means we don't have any payment logic yet (just showing them a mockup price).

Once the user has been to a stage, then dont go back to a previous stage (even if they might redo some work there). So once the user is shown the preview the preview section will never disappear, even if the user might decide to re-spec the website.

## TCP Ports

We need a some complex port management here. First, we need the port for BE and FE (in prod we won't need FE port, but in dev mode we do). Also, since each project is a full on webapp for them, we need ports for them as well. Here's the scheme:
Each project has an id (it autoincrements, so project id goes like 1, 2, 3, ... etc). We'll assign ports to project with ix X as follow:
- BE port: 8000 + X
- FE port: 16000 + X
Or something like that (the details can change). This also means a server can host less than 8000 projects. We'll scale by adding more servers later.

Each claude session also needs a port for the MCP (that's how the BE can send requests to the MCP). This port will be dynamically assigned (meaning it could be different each time). Every time we need to start claude for a certain project, we choose a random port, and if that port is busy, we choose another until we find one that is not busy. We save that port temporarily to the DB and it will server as the channel id so the backend can identify the channel of the claude session (the channel meaning the communication MCP).

## Development environment

When a project is started or initialized, a new directory <root directory>/<user hash>/<project hash>/<conversation id> is created for that project (for now each project will only have 1 conversation).
Each project will have 1 directory like that. The structure of the directory:

├── CLAUDE.md  -> the main instructions to the agent
├── artifacts   -> artifacts the agent generated for the users which they can view in the Documents tab
│   ├── erd.md
│   ├── flows.md
│   └── pages.md
├── backend       -> the backend code directory, which is initialized from the boilerplate
│   ├── CLAUDE.md   -> the claude instruction specifically for the backend coding convention
├── frontend     -> the frontend code directory, which is initialized from the boilerplate
│   ├── CLAUDE.md   -> the claude instruction specifically for the frontend coding convention
├── logs     -> BE and FE application log, so sometimes claude can check what's wrong with the project preview servers
│   ├── be.log
│   └── fe.log
└── uploads    -> user uploaded files will live here
    └── 81016e96_Screenshot_2026-10-13-22-22-25.png

CLAUDE.md and backend, frontend are prepared when the platform is setup. For each new project they are copied over that new project's directory.

When the user transitions from the spec-ing phase to the implementing phase, the agent will continue to write codes in backend and frontend, then it will starts the frontend and backend server (using pm2).

So:
- In spec-ing mode: directory created, claude session online.
- In implementing mode: directory created, claude session online, project servers online.

To avoid wasting resources, the claude session and the server processes of each project don't have to be on all the time. Projects that haven't been visited for some time can have their session and servers turned off to save resources. Currently it has to be done manually but later we will design a mechanism to automatically turn off stale projects.
- Currently we can only withstand 2 projects at a time on the BE. To start a new one we'd usually need to turn off the existing ones. Turning on for 1 project is automatic though (the BE makes sure the claude session and the project servers are on whenever the user visits a project).

## Domain name

Using a temporary domain: vib3.tuvithanso.org. No name yet.

main app domain: vib3.tuvithanso.org
api base url: vib3.tuvithanso.org/api
project preview domain (for each project to be viewable by the user when building): <project-hash>.dev.vib3.tuvithanso.org
project preview api base url: <project-hash>.dev.vib3.tuvithanso.org/api

Note that all ssl certificate work is already handled, even for the project previews.

## Pricing structure:

$49 -> $39 for to get the page, $29 -> $9 a month for hosting.
- Users can choose to host it somewhere else, if so they will only pay for the site, can download it (as a zip file) and deploy to another server. We can give them some instructions.
- On domain name, if they choose to host with us, they can either provide their own domain name, or brainstorm with the agent (it will provide some options along with the price) to choose one. We can offer to give them a domain for free for 1 year (if it's below $20). Otherwise they pay in full or use their own.

# Todo

Design a security system so claude sessions are contained and cant be exploited.
- Suggestion: create a new linux system user for each platform user, and it only has access to the user's directory, nothing else (<root directory>/<user hash>). The user's claude sessions will be started under their corresponding system user.
- Also take care of guard rails against prompt engineering.
- Also ask claude to do a system-wide pentest after everything is done.

User onboarding flow (asking basic info (or maybe skip them), if the user has any experience with web building - this info can make its way into the instruction prompt).
- Also make them feel welcomed/hype them up?

Include admin page building/questions into the building flow.

Include data import handling/questions into the building flow and provide the mechanism. Can give users the option to ask for human support.

User experience when building their website (comfort, guidance, encouragement, engagement)
- Might in the form of tips/help messages, agent-based encouragement or sweet talking, sensibe building flow, or good UX.

Deployment flow (domain name, prod deploy), including techincal and business flow design.
- Also maintenance flow, how users move forward with the site once it's deployed, regarding fixing bugs and making update, and redeploying.

Proper user profile section, and potential email verification.

Payment, payment method management, billing/invoice management.
- Let's go with Lemon squeezy for now.

Proper terms/policy/any other documentation.

Decide on the branding and then get the domain.
- Also brand visual identity (logo, color scheme, website aesthetic)

Setup google login.

Landinge page
- Being coherent with the main app.
- SEO

Stress-testing the site (especially session starting and it being robust)
Setup logging and alarm, and a workflow for us to check system's health.

LLM provider
- Might go with deepseek right now. This would also resolve the window limit issue.
