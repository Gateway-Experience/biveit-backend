# Overview

This will be a web builder like Lovable but geared towards people with little to no web building or programming experience. So the process and instructions will have to be super clear, friendly, guiding and easy to follow through.

The websites users'd build should be landing pages, small to medium business websites, not full on saas platforms.

## Criteria

- Friendly & easy: eli5-ish (but not too patronizing), avoid jargon, casual and illustrative writing style.
  - We could adopt a 2-stage explanation style. Just explain briefly and casually first and assume they can understand, and if the user needs more detailed explanation, go heavy (give them examples, analogies, explaining in more concrete terms and in more details)
  - Cut out everything that they don't need to know. Don't over-explain.
- Lots of guidance: there might be some technical parts where they wouldn't be familiar with. Docs or tooltips should be available to explain to them in layman's term, just make sure they're not too invasive.
- Effortless: try not to ask them too many questions or make them spend too much mental effort. Although we need to balance that with grilling them to get the clearest spec possible.
  - Giving them ready-made options with suggestions is probably the best way I can think of rn to reduce users' mental workload.
  - Have a lot of hand-holding in the process so they only need to make decisions where it matters, the rest is taken care of under the hood.
- Allow for deep customization and specifics: if users want more control of their website, give them the means to do so.

Although easiness is our main selling point, creating a proper website that conforms to what the users want (especially with some business flow) takes time. We'll frame it as you can create it and have it online inside 1 day, compared to hiring a freelancer which would take days, potentialy weeks and a lot of back-and-forth until the proper site is finished. So some effort is required although we'll try to minimize this.

# Binary classification

Main classification principle. When you wanna categorize something, it's best to just split it into 2 categories. It's simple but powerful enough.

E.g. for the web building process, we could split some related items along the following axes:
- Simple vs complex
- Formal vs casual
- Foundational vs details
- Common vs specific to the domain

# Building process

Probably separate into stages, since it's easier for users to grasp:

- Spec-ing: gathering and clearing out requirements from users as best as possible before building starts. If they themselves don't know what to do in some aspects, give them suggestions and guide them.
- Implementing: building the web site with previews
- Deploying: guide them through the process of getting a domain and deploying to a server. We also provide these services btw.

Each stage could be iterative on its own, and the user could go back and forth between stages, but the general objective is to get them through the process in a waterfall-like manner. Ideally they go through the stages in 1 clean sweep, no looking back.
The goal is to get to 80% during the first pass (e.g. getting business domain requirement or confirming site flows), and the other 20% is in the form of tweaking or further requirement discovery.

## Synchronicity between stages

Really this is just between spec-ing and implementing, since deploying is quite independent from the other two).

We maintain a spec of the website to be its source-of-truth reference. But the users dont need to know about this as a concept, when they view the spec they will think they're just viewing a document about the website they're making.

However, spec document (in 1 md file) is product of phase 1 (in the same way the final source code is the product of phase 2).

When any changes, tweaks or updates in any phase contradict with the existing spec, it's silently updated into the spec. Care is taken so the spec stays consolidated and not bloated/disintergrated after many changes.
Categorize the changes requested into 2 camps:
- Small enough changes, or conforming enough, that can be considered "incremental" and can be made, spec silently updated if necessary.
- Big enough changes that might require some rework of the current state so far (rewriting the whole or a large part of the spec, rebuilding the whole site etc). For this, let the user know, give them some briefing and ask for the their decisions.

There will be a prompt to instruct the agent on this matter in concrete details.

The goal here is not forcing them through the typical and rigid software development process, and at the same time try to be as conforming to what they have in mind as possible.

## Spec-ing

### Axes

Visual or logic

Simple/trivial vs complex
- This is judged by the effort needed to make the change/implementation. By nature visual/UI changes are more likely to be simple than logic changes (e.g. make this button bigger), but there could be both simple and complex changes in both types (visual and logic).
- Might be judged based on independence level as well. Isolated changes are safer and easier to make because agent doesn't have to consider the changes' implications.

Common or specific to the domain
- This is about whether the thing in question applies to many or all projects (e.g. should we forego user signup) or specific to the project (.e.g. should we allow refunding bike rent)

Foundational vs details
- This is on whether the change touches on a foundational issue, e.g. should we have a common page that has all the interactions/features or split them to multiple pages, versus things like should we show the tooltip here (this is just an example, it doesnt have to be UI related). This is different than common or specific to domain. Foundational vs details can all be domain-related questions, or all common stuff.

Essential or customization
- Either something needs to be clarified for the process to move forward, or it could be left till later, or even dropped.

### List of things to ask/decide

Theme: dark/light or both?
- Axes: visual, common, customization, simple
- Default: light theme, can't switch

Page type: landing page, blog, portfolio, mini platform (with interactions and crud stuff), etc. or a combination of those things?
- Axes: essential

Has user login?
- Axes: complex, common, essential, foundational
- Default: yes

Has social login (google only for now)?
- Axes: complex (slightly), common, customization
- This is only relevant when the answer to the question above is yet
- Default: yes

Choose from some preset theme
- Axes: visual, common, essential, simple
- Default: we'll define a default theming config
- We'll present this before the dark/light theme one, so users should have chosen a main theme already with this. The other question essentially boils down to whether we support switching or not.

Color palette/scheme
- Axes: visual, common, customization, simple
- The preset theme should contain colors already but user can customize

### Main flow

1. Bootstrap form

Will be split between 1st-time users and recurring users. Below is the normal form for recurring users.

The purpose is to try to gather as much info about the project as possible, before AI is event involved. Only the project desc is required, everything else if optional.
The agent will consider the answers and see what they still need in the subsequent chat, and kinda guage the user's specificity and interest.
Communicate clearly that they should try to be as complete and detailed as possible, but it's fine if you dont know what to do decide at the moment and the chat will pick them up later.

We show them a form to bootstrap the project:
- What is the project about: write a description in a quite detailed manner. We'll provide them with some examples so they have an idea of what it would be like. Mostly the business model and domain, a bit about the website's spec.
- Whether login is needed
  - If yes, whether google login is needed
- What kind of page it is (multiple choice with option to type)

As for 1st-time user, we'll only ask them to provide project description, and not in as much details as the normal version. No other fields.

2. Theming form

This form is where we'll bootstrap theming and styling for the project. The user is instructed to choose and configure a style as close to what they like as possible, while reassuring them that's all they need for now and they can customize it further later on.

- Asking them for a description of how they'd like the site to look like (giving them examples and tell them to just say whatever)
  - This and the project description gets fed to a haiku model for choosing some nice preset.
- Asking them for a primary color. Derive a color scheme based on this and provide to them for approval.
  - Show some demo mockup with the color so they have a feel.
  - Assume light theme here.
- Asking them if they like dark/light mode switcher (only for recurring users). Then automatically derive the opposite theme color.
  - No need to ask for approval here.
  - Only show when the color scheme choosing is finalized.

3. Chat

TODO: details in each section here.

First the agent will try to get the intention and the business model as clear as it can from the users. This might involve some site structures info as well as the question of login (because it's relevant to the business).

Then it will try to build out an entity/relationship spec and user/business flow design, what a development team would need, but then try to confirm that with the users by rephrasing it in a less techincal and more casual terms.

Once this is confirmed, the agent will try to present pages make up (what the pages are, what are in each page like list, form, main buttons etc), and the page transition. Again discuss and go back and forth with user on this.

Once all is done, the agent should now have a detailed spec containing ERD, business/user flow, pages make up and transition, a list of decisions (TBD: DAG form) and any nuances.

## Implementing

The first step in this phase will be a full website built from the spec, with everything already decided built and ready for use. Including BE. Theming and styling should look finalized, not looking like a mockup. In short, there should be a chance the user uses this version right away.

Most of this stage is agent asking if there are anything the users want to change/update, and iterate with them until they're happy with the site.

Encourage the user to use/test the site directly in the preview area.

Only when really necessary, go back to the spec-ing stage (which would hide the preview), and choose a substage in the stage to start over from. Confirm with the user when you're going to do so.

When the user gave the overall approval, move on the deploying.

# UI

## General controls

Users mostly chat with the AI agent in a conversational manner.

Multiple choice with an option to type something (like what Claude Code has). There might be 1 or several of these at a time. We could group them for now (like Claude showing you 5 questions then letting you cycle through them), but I think we could just show users one by one at first so they're not overwhelmed.
- Users can also cancel the question. If the agent were going to ask them a bunch of questions, cancelling should make it stop asking further questions, but it should know it were going to ask them before the cancellation (so e.g. it could ask them again later).
  - I'm thinking an 'X' icon on the question panel, with a tooltip saying smt like "Dismiss the question and go back to chat mode".
- Of course users cant chat when presented at a question (they can only answer the question or dismiss it).
- There could be a suggestion under the list of options.

Arbitrary control: a generic control where the user clicks a button in the chat, a dialog opens up where the user could do something on it, then when it's closed an output is returned to the chat.
- This is a generic mechanism so later we could define any control we want.
- An example I have right now is to let users browse through a list of website styles. On the dialog they could a carousel of styles and how they look. When they choose one the agent will receive that option.

Artifacts:
There should be a tab or section to list all artifacts generated. On the chat screen all artifacts (e.g. the spec document) are presented as a button or smt and link to that artifact in the artifact lists.
Since we dont have enough space on the screen, the artifact will be viewed in place of the chat. Each artifact viewing should have a button to go back to chat though so it's easy for users to find their way back.
Artifact is an LLM term but I find it unfriendly so maybe we'll call it `Documents` on the FE?
*Later on* we could insert the decision tree (or DAG graph) here, to let users review all decisions made so far.
Btw I think we're not showing the users any code at all, unless they specifically ask for it. Even so we'll try not to disclose too much internal details.

## Spec-ing

There will only be a main chat screen.

## Implementing

There will be a chat screen on the left, and a preview on the right. The preview will be on a 4:3 resolution so there might be some space above and below the preview area for some preview controls and buttons.
