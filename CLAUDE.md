ANH'S CODING PRINCIPLES & BEST PRACTICES:
========

Below are the coding principles, conventions and best practices that you must follow when designing techincal solutions and writing code. Some might apply to long/meaty tasks like designing a whole system or mechanism, or writing large amount of code; some might apply to making small changes or the actual coding act inside the bigger tasks (like dont mutate a variable).

You must follow them as closely as you can. But be sensible about this. Exceptions can be made, but only with very good reasons, or in low stake cases like doing a quick test or writing a small POC/script. But in general, there is nothing to lose by following these principles and practices.

Sometimes, when working with an existing codebase, you can adopt the existing convention (e.g. boilerplates, module arrangement), if it's not too conflicting with the principles here. Even so, we should stick to our principles as much as we can. For example, we can adopt the convention of creating a static class for persistent logic (even though they're better off as independent functions), but we don't have to follow the existing imperative practice of modifying the array inline (e.g. array.push in js) and instead use something declarative (like spreading notation).

----
PHILOSOPHIES:

Here are some high-level philosophies that will aid in how you approach a problem or design a solution. Mostly they're considered when planning/doing system design, although sometimes they could be applied to the actual coding.

## Topology

Borrowed from the mathematical branch of topology: many designs are functionally *the same shape* (same topology) — the difference is only which shape is most convenient for the situation at hand.

- Don't agonize when choosing among functionally-equivalent options. They're equivalent in *what they can express*; they differ only in **convenience and efficiency**, and that depends entirely on the actual scenario the system is in. No silver bullet. Design then reduces to **choosing the best shape for the job** — provided the topology isn't *corrupted*.
- Example: storing custom fields in a JSON column vs. a separate key-value table — functionally the same, same topology. Normalized vs. denormalized schema — same topology. *("It's the same topology anyway, man.")*
- **Refactoring** keeps the topology *the same* — nothing added or removed — and just reshapes the codebase into a more aesthetic shape. (Contrast this with *collapsing* topology below, which genuinely destroys information; refactoring never does.)

**Collapsing topology** is the failure mode to watch for. You collapse (corrupt) a topology when you write a *derived* value into the place originally meant for a *base* value — e.g. computing a user's effective subscription plan and overwriting their original plan with it. The original is now *lost*; the informational shape is permanently reduced. Compare to writing the derived value somewhere else, or not persisting it at all (recompute each time) — both preserve the topology. **Watch for any place a design overwrites source information with something derived from it.** This is the same idea as base-vs-derived under [Declarativeness](#declarativeness), seen geometrically.

## Synchronicity (or the absence of the need for it)

**Avoid syncing between two data sources.** Maintaining consistency between sources is the mother of most bugs. Whenever a design introduces syncing *in any shape or form*, stop and reason about why it's necessary — and whether it can be reduced to a single source.

- The only legitimate excuse is **efficiency or convenience** — e.g. multiple indexes over the same source. A CDN is a kind of "physical index" as opposed to a logical one. But that's infrastructural; in ordinary application code you usually should *not* be deriving and persisting parallel structures. (Shares ground with [Topology](#topology): a redundant copy is a second shape claiming to be authoritative.)
- When syncing genuinely *must* happen, it must go through a **guaranteed mechanism** that gives you eventual consistency *at the conceptual level* — transactions, or event logging. Keywords to reach for: **append-only log, monotonically increasing sequence number / watermark, idempotent**.

## Declarativeness (as opposed to imperativeness)

- **Pure functions, as much as possible.** A function's signature should make sense *on its own*, read in isolation, divorced from where it's called. (Caveat: "makes sense" doesn't mean "tiny" — don't shred the design into a swarm of micro-functions; reserve small functions for genuinely distinctive or repetitive utilities.)
  - **Statelessness** follows from this and buys you reasoning, predictability, repeatability, and a path to **idempotence** — which is one of the great comforts in software.
- **Compute next actions from current state — don't precompute future state.** A bad, over-reactionary design precomputes future state in a precipitous way (over-fitting to the requested feature), producing derived state that drifts and goes buggy. Instead store *the current state as it is — no more, no less*; that, plus the input at invocation time, is enough to derive every output that could ever be needed. (Note how much **abstraction skill** this takes — designing is partly the skill of seeing what the minimal true state is.)
- **Immutability.** Prefer not mutating a variable or object/array. If a value flows through stages, give each stage a new name (`data` → `data_with_users` → `data_deduped` → `data_deduped_enriched`). **Exception:** performance — and it must be *intentional and commented*. Cloning arrays of tens of thousands of elements is real cost; when you mutate for that reason, say so clearly in the design.
- **Base information vs. derived information** — this parallels declarative-vs-imperative directly, and it's arguably the spine of this whole document.
  - *Base* information is the most declarative thing there is: it's closest to the real world or the source systems, and declaring it *in full* is **complete** — every derivable fact can be reconstructed from it. Storing *derived* information feels imperative; defining only the *derivation rule* from base information feels like writing a pure function (declarative).
  - **So, when designing, present the base information first. Model the world as it is** — rather than being imperative, over-fitting to the immediate feature, and losing sight of the source truth underneath.

## Duality of table and stream

A **table** (or state) is data *at rest* — collapsed, the accumulated result. A **stream** (e.g. a change stream / event log) is the same data *in motion* — the sequence of changes that produced it. They are two views of one thing, and you can convert between them: fold the stream and you get the table; diff the table over time and you get the stream.

This duality is a design lens, not just trivia:
- A table is the *derived* view; the stream of changes is closer to *base* information (it's append-only, nothing overwritten — so it never collapses topology). When you keep only the table, you've folded the stream and **discarded history** — sometimes correct (you don't need it), sometimes a topology collapse you'll regret.
- When a design needs auditability, replay, eventual consistency, or "how did we get to this state," reach for the **stream** form (append-only log + watermark + idempotent consumers — same keywords as [Synchronicity](#synchronicity)). When it needs fast point-in-time reads, reach for the **table** form. They're not in conflict; you often keep the stream as the source of truth and treat the table as an index over it.
- Ask of any stateful design: *am I storing the collapsed result, the sequence of changes, or both — and is that the shape this job actually needs?*
----

----
CODING PRINCIPLES:
- Colocation: related code or functions and related helpers/parameters should stay as close to each other as possible. Also relatd states under a domain should stay centralized instead of scattered (e.g. think global redux state).
- DRY: if some logic feels high-level or is repeated a lot, extract to a common function/module. This requires some thinking though, since just because some code look similar doesn't mean they are the same and should be extracted. This depends on how generic the code is (high level code, utilities, or foundational; or specific and use-case dependent). Sometimes this is more clear after the code is written, so you can always come back and decide what should be extracted out. One rule is that if the code has to be modified a lot to be extracted out, then it shouldn't be.
- Avoid OOP as much as possible.
- Separation of concern: when designing functions and modules, try to think if any item can stand on its own, and encapsulates enough (no more, no less) logic. It should function nicely if another use case need the same functionality from it and can just use it right away because there is no other unrelated logic got mixed up in. For example, if a transportation platform uses pathfinding logic, that pathfinding logic should stand on its own so it's can be readily reused in a computer game.
  - Colocation and Separation of concern are 2 most important principles when deciding where to put code (either file-level or routine-level).
  - Again, separation of concern is very important and you often get this wrong. When designing where code goes you should think hard. For example, a deduping mechanism using redis should have generic redis operations in 1 file/module, and the deduping logic that makes use of the redis operations in another file/module that is more logic-oriented.
- All the other principles and advices should be taken along with this one: don't over-engineer. Don't overdo any advices. Simplicity is best. Use your common sense.
----

----
COMMENTING:

Types of comments that you should be writing:
- Showing intention: also starts with `Intentions:` e.g. `# Intentions: ...` (depending on the language). This says what the code does. Doesn't have to write comments for every line of code, but for a meaningful block of code, or a function, that shows the tangible intention of the writer. The reason it exists so to speak. This applies so strongly to for example a hacky variable or logic, the reason they exist were to solve such and such problem, otherwise they wouldn't exist at all. Also in general the intentions should help with narrating the code so it's easy to read the code as an outsider.
- Referencing: association between information is important for them to be connected. You can reference another code snippet, a function, an url, a task etc. E.g. this logic is related to that logic over at the other section. Use `References:` preamble e.g. `# References: ...`.
- Original purpose: use e.g. `# Original purpose: ...` to describe the reason why this function or code was generated (e.g. the feature being implemented at the time, or the API endpoint that it serves). This helps a lot when the human is reading code to know the context of the code's existence or what flow it belongs to. The majority of code written only serves 1 purpose anyway. That being said, dont use this comment directive for generic code (based on your judgement) that could be called in many places, even if at the time only 1 flow uses it. Generic, utils or core functions are such examples.
- Parameters/variables: sometimes you can say the function/method does X, but the parameters might not make too much sense or are ambiguous. For example source and destination param of a function can be confusing to tell which is which. Also some variables have special purposes like for keeping track for a recursive-like logic, acting as output store (for some languages like C++) etc. If their meaning is not immediately apparent, comment say what they are and/or how they are used. Being brief is fine on need to write a lot.
- Whether a function is pure: can just be a 1 liner comment e.g. `# [Pure]`, or can say it casually in another longer comment. `[Pure]` means completely pure, no I/O and mutation, purely in memory and like a math function. `[Pure-ish]` means generally pure, could do read I/O, but nothing serious happens in the real world (even maybe some logs get written but as long as they dont affect the main logic. But if the log is the main purpose of the function, then writing log is not pure-ish at all).
- IMPORTANT: for the writing style, write it as if you're talking to a person. Make it sound casual, easy to absorb, and not cryptic. Comments are meant to be read by human. Also make them concise and not wordy, while styling sounding human. Dont put context that's too specific to the immediate context in the comments. This needs some judgement and taste though because things decided in the moment could actually be relevant and should be put in the comments. But just know that there is a knob and you have to decide wisely whether the immediate context (e.g. from discussing with the human) should be put in the comments or not.

DON'T write comments for the sake of having them. If something is obvious from the code, don't put comments in. Don't say:
```
# Looping over array extracting x
extracted = [item['x'] for item in items]
```
Be sensible. Imagine a stranger reading the code. They don't need to know every little details (except for the non-trivial ones that could make or break the system).
Comments don't have to be huge paragraphs either. Be brief. As long as it contains enough information and is doesn't feel obscure when reading.
Make sure to write comment for hidden implications or gotchas.

Avoid generic naming. Names like `expand`, `field` are too ambiguous. Some intention and context should be in the names themselves (just make sure they're not too long and verbose). Generic names like above are acceptable only when the context is clear (e.g. a member of some upper entity that contains the context already), or are truely generic.

Comments shouldn't be too formal or verbose, should be easy to read, e.g. can adopt blogging writing style. Also don't be too brief to the point it's obscure. Should feel like a reddit comment: casual but explanatory.
----

----
DESIGN PATTERNS:

This section lists some design patterns that might be helpful. Use common sense to pick what's best depending on the situation.

Type-ID combo:
Easiest way to make a system/feature extensible. E.g. actorType + actorId, objectType + objectId. To extend to a new object/entity type, or merging data from a new system, just define new types. Becareful not to overdo it if the type classification is trivial (e.g. we know there's only 1 type) or when the need for extending is unlikely.

Indexing a dataset:
There 2 main ways of indexing into an element within a structured dataset:
- Path-based (e.g. file path). This requires the data to be structured as a graph-like or tree-like structure. B-tree itself is a path-based index since we traverse from the root node to the desired node. So is zone-mapping (there is a hierarchy of sort involved).
- ID-based: pointing to the exact element, by passing the hierarchy. Example: css selector by id, labelling entries (so they can be pulled directly by labels), hash tables.
A dataset or database equipped with these 2 indexing mechanism is usually good enough for most cases.
An example: a text-based knowledge base can index the knowledge items by paths (file path and then maybe section name of the item), or by IDs (e.g. tagging certain items with a unique identifier, inline).

Sharding:
Apply in any situation that O(f(n + n + ... + n)) < O(f(n)) + O(f(n)) + ... + O(f(n)) (kn is sharded into k shards of size n).
E.g. A join B on x. The value space if x (say of size kn) could be sharded.
Before sharding: A join B ~ O(k^2 * n^2). After sharding: A join B ~ O(k * n^2)
----

----
Module imports should be put on top of the file. Dont import in the middle of a function unless there is a very good reason to do so. Also import builtin/installed libs/packages first then come the project's modules.
Private functions (not exposed to external files) should be prefixed with (at least 1) underscore.
Function/method names should always start with a verb (unless there is a very good reason to do otherwise).

Python-specific conventions:
- Use defensive programming (e.g. pydash if available).
- Prefer async over sync for I/O tasks/logic. Use async whenever possible.
- If there's an __init__.py file in a directory, put comments/docs in it explaining the purpose of the directory and what should be put in it (or what shouldnt be).
- Comments on the whole function or type (or something like that) should be docstring so it can be conveniently referenced (can still put `Intentions` etc in). No need for individual or inner comments.
----

END OF ANH'S CODING PRINCIPLES & BEST PRACTICES
========

## Current tech stack

Python (usually 3.12) with uv + FastAPI & asyncio.
MongoDB.

# NEW DESIGN AND ARCHITECTURES

For the backend rewrite.

We will use FastAPI with this common architecture:
controller -> service -> repository (allowing controller -> repository where it makes sense, e.g. when the service is just a thin wrapper).

Controllers will contain the endpoint definitions and endpoint/param-related processing.
- This could include other kinds of interfaces with other subsystem e.g. kafka consumers, not just for http wrapping.
Services will be business logic-dense, and is agnostic of the fact that REST-based controllers call it, and could mean other form of subsystems in different protocol could call them. Services can have a dag-based topology (e.g. services calling other services).
- Inside services, if there are well-defined features or groups of features, or modules, they can have their own sub-folders inside services. For example, `users/` folder inside services and contain user-related logic (authentication, user profile management, user crud etc). For common files or files you're not sure they could be isolated into submodules/folders yet, leave them in the top folder (which is `services`). Keep in mind that this is only for services. Relevant controller or repo files for the same feature/module doesn't have to follow this grouping in their own layers. Although it could take inspiration from it (but I imagine this would hardly be the case, as repo files are arranged by db tables). For example, there is usually only 1 users.py file in repo layer that takes care of all operations dealing with the users table.
Repositories deal with data-related logic (storage, retrieval, mutation). Any business logic that is tightly coupled with data manipulation should live here instead of trying to prying them out of data operations to put them into services.
- Structure inside repositories should be quite flat and should somewhat mirror the actual table/collection list in db.

Top-level folders in services/ could adopt a horizontal structure (i.e. each submodule or feature-set could occupy a top level folder in there).

So we have a this overall directory structure:
```
src/
  main.py (and whatever top level files for entry points)
  controllers/
  services/
    users/
    ...
  repositories/
```
Sometimes there could be isolated, standalone modules that are independent enough that they are promoted as such, to be completely enclosed, exposing an interface through __init__.py (so other callers would import the module as a whole instead of individual files). The action/action items module is one such thing.
- For modules like these, the internal structure could also adapt the controller/service/repository architecture (although controller might not be necessary in most cases). I originally wanted to put all repo files in src/repositories/, but it makes more sense to put the repo files of a module inside its territory. So we have:
```
src/
  main.py (and whatever top level files for entry points)
  controllers/
  services/
  repositories/
  modules/
    <module name e.g. actions>/
      __init__.py (interface declaration/wrappers)
      services/
      repositories/
    <another module>/
```
Decision to promote to a standalone module comes from the human although you could suggest if you really think it makes sense to do so. Never decide to create a new module by yourself.

Aside from that, there is utils/ folder for any common utilities, and a constants.py for any constants/enums. Unless there is a very good reason for the constants/enums to be local in the files/folders that use them, they should live in constants.py.

All logging should functions in utils/logging.py, including all error logging utilities.
- Error logging should be done only at the top level (e.g. API handler in the controller, or top level of event consumption). Inner functions just have to raise an exception. Error logging could be done by inner (e.g. services) functions though if it swallows the error, breaking the bubbling chain.
- In http controllers, use log_http_error(). In services, use log_error() (if eligible).
- Use `logging` library very sparringly, and almost never for errors (unless there is a very good reason!). Use the above functions instead for errors (and following the guidance as well).

Again, write declarative code, and avoid mutation (e.g. list.append) as much as possible.

# CONVENTIONS

These conventions you should follow during implementation planning and coding, and used for self-reviewing. Splitted into rounds specifically for the reviewing skill but when coding you should treat them all as 1 set so you apply them all properly.
Usually the rounds can be applied in the presented order when reviewing (they show up in the order of more perilous first, and more tamed last).

Also, there might be comments with the format `# [A] ...` in the codebase. These are additional notes, instructions and examples for you (A is for agent), that might provide additional conventional context or reinforce good habits. Look out for them if you can.

## Round 1: Some logic-ful conventions

Timestamp & timezone:
For datetime, always store UTC in db.
For date-only fields, the tz is dependent on the context (could be a separate tz field, or implicit in the code). The date field contains no tz information by itself.

PK:
Every table should have its primitive PK (mostly bigserial or bigint for SQL, or ObjectId for mongodb).

We value eventual consistency. As long as we some flow has eventual consistency, it's usually good enough. No need for over-optimizing for correctness.

## Round 2: Structural

You can follow the conventions in this round by refactoring existing code (splitting/merging functions/logic) so that they get put in sensible places. This round is about organizing code and not about changing logic/approach. Separation-of-concern principle might be of interest here. Below are the conventions.

Service layer shouldn't be aware of repo internal (e.g. whether it's Mongodb or Postgres etc) at all, those should be the repo layer's teritory. Service can invoke repo but should be agnostic of the internal details as much as possible (e.g. which db engine is being used). It being aware of them (e.g having mongo or pg name in some function calls) is code smell.
By the same token, logic in repository should be about retrieving or persisting only. Some business logic live here though since it deals with query logic/optimization, fine. But be doubtful if any application-level logic that doesn't help with db operations stays in the repo level (e.g. elaborated output transformation or orchestration).

If a function/class/method (private or public) has an overview comment describing what it does or any overall note about it, it should be a docstring instead of a plain comment. This helps with code navigation and development.

Don't overdo data typing, only do them for official entities and important interfaces (e.g. API, subsystem), not on everything like common utility functions or internal functions.

Use pydash extensively instead of writing small & trivial utility functions. Also, don't create small private functions that could be a one-liner with some array/string manipulations or some pydash calls. Excessive function navigation makes reading code hard.

When running migration/test/probe etc scripts, if there is the need for a dry-run mode, use the convention of using `--apply` cmdline parameter to apply, otherwise without it the default is a dry run. For scripts without the need for a dry-run mode (non-destructive or without side-effect), no need to worry about this then.

Any non-credential or non-env-based config you add must be in constant file (e.g. in code), so you'd minimize adding env config

## Round 3: Naming

Repository function naming:
- `find` or `find...` instead of `search...`.
- Naming in the repo layer (and in general) doesn't have to be too verbose.
- `find` for something with more elaborated filtering and often returns a list of items. `get` or `get...` is for getting 1 item (usually by id or some unique field, tenant_id might be included as well). For getting all records, use `get_all` name.
- other common crud function naming: `update`, `update_one`, `delete`, `insert`, `insert_many`
- repository functions shouldn't be too verbose for a specific need (unless the logic and the purpose itself is very specific). The general rule is repo functions should be reused in as many cases as possible.
Bad:
```
shop_repo.find() # returns 5 fields
shop_repo.find_for_filter() # returns 2 fields
```
Good:
```
shop_repo.find() # return all fields, for both use cases
```
Why: too over-optimized in terms of field selection. 1 generic find function reused for all similar use cases is fine.

The how and what is done should almost never show up in the naming. It should be obvious from the params, or if not, it should be the function content's concern. Unless the how and the what have some business meaning in them and should be made clear.
Bad:
shops.find_filtered_by_meta(filter, meta)
Good:
shops.find(filter, meta)

File in each layer (repo/services etc) should be based on the entity, not the action. E.g. services/shop_connections.py, services/shop_manage.py should all be in services/shops.py for clean grouping. Only consider spliting if the file is large enough (e.g. > 1000 lines). Otherwise keep them in 1 place.
Bad:
ams_subscription_setup.py
Good:
ams_subscription.py
Why:
the `_setup` is a verb and shouldn't be in the filename. Imagine later we add some read or report logic in ams_subscription.py, then it's not purely about setup anymore.

Bad:
services/shop.py:
```
async def get(shop_id):
  shop = await shop_repo.get(shop_id)
  if not shop:
    raise NotFound
  return shop
```
Good:
Use shop_repo directly. The caller checks for existence themselves.
Why: thin wrapper in services. Bad practice. Use repo directly for simple CRUD functions (but if say, update op has some more validation logic instead of pure pass-through, it should stay in service layer).

# LINT AND TESTING

After a round of changes, you should do sanity check by running these:
$ uv run ruff check --fix
$ uv run ruff format
$ uv run pytest tests