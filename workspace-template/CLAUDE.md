# Knowledge workspace conventions

This repository is an Open Knowledge Format (OKF v0.1) bundle maintained by the Vexa agent from meeting transcripts. A folder of the owner's Obsidian vault is a read-only mirror of this repo, so everything here must render well in Obsidian and must stay consistent from one run to the next. Follow these rules exactly.

## Layout

- One markdown file per entity at `kg/entities/<type>/<slug>.md`. Types in use: `person`, `company`, `project`, `meeting`, `decision`, `topic`. Slugs are lowercase, ASCII, hyphen-separated, stable forever (a person is `firstname-lastname`, a meeting is `<YYYY-MM-DD>-<platform>-<native_meeting_id>`).
- Every entity file starts with YAML frontmatter. Required keys: `type`, `id`, `title` (`id` equals the slug). Recommended: `description` (one line), `tags` (list), `timestamp` (ISO 8601 of the last update), `resource` (a URL to the system of record when one exists). Always include `aliases` (a list; start with the title; add every former title when you rename) so links survive renames.
- Cross-reference with `[[wikilinks]]` by title, exactly as the target's `title` (Obsidian resolves `aliases` too).
- `kg/entities/<type>/index.md` lists every entity of that type as `- [[Title]] - one-line description`, sorted by title. `kg/index.md` lists the type folders. Update the index of a type whenever you add or rename an entity there.
- `log.md` at the repo root is the change log, newest first, grouped under `## YYYY-MM-DD` headings, one line per change with the entities touched.
- `uploads/` is the inbox: transcripts arrive there as `<date>-<platform>-<id>.md` with a `type: transcript` frontmatter. After you have folded a transcript into the graph, move the file to `uploads/processed/` (same name). Never edit, summarize into, or delete files under `uploads/processed/`.
- `Dashboards/` holds Obsidian Dataview queries. Never edit it.
- `README.md` is the owner's dashboard: keep its counts and "recent" lists current when you change the graph.

## How to write

- Facts are dated and attributed: every fact you add to a person, company, project or topic cites the meeting it came from as `(from [[<meeting title>]], <date>)`.
- Before creating an entity, search `kg/entities/` for an existing one by title, alias or obvious variant; update in place when it exists. Never create a duplicate entity for the same real-world thing.
- Create the target file first when it doesn't exist yet (a minimal one with frontmatter and one line is fine), then link; never link to an entity that does not exist.
- Keep the owner's own entity (`self: true` in its frontmatter) as the point of view: "we", "our" refer to the owner.
- Do not invent. Record only what the transcript says or what the graph already holds. When the transcript is ambiguous, say so in the meeting entity's Open questions.
- Plain hyphens only; never typographic dashes.

## The meeting entity

Path `kg/entities/meeting/<slug>.md`. Frontmatter: `type: meeting`, `id`, `title` (`<YYYY-MM-DD> <platform> with <participants>`), `aliases`, `date`, `platform`, `meeting_id`, `native_meeting_id`, `duration`, `participants` (list of `[[Person]]` titles), `tags`. Body sections, in this order, each present even if it says "None":

```
## TL;DR
## Key points
## Decisions
## Action items
## Open questions
## Attendees
## Companies
## Projects
## Topics
```

TL;DR is two to four sentences. Key points are bullets. Decisions are bullets, each also filed as its own `decision` entity linked here. Action items are checkboxes `- [ ] <what> (owner: [[Person]], due: <date or unknown>)`. Attendees, Companies, Projects and Topics are bullet lists of `[[wikilinks]]`. Do not paste the transcript into the meeting entity; the transcript stays under `uploads/processed/`.
