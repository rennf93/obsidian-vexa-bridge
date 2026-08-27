# meeting-to-graph

Run this whenever the routine fires. Read `CLAUDE.md` first; it is the contract.

1. List the files directly under `uploads/` (ignore `uploads/processed/`). If there are none, stop and reply "inbox empty".
2. For each transcript, oldest first:
   1. Read its frontmatter (`meeting_id`, `native_meeting_id`, `platform`, `date`, `duration`, `participants`) and the timestamped lines.
   2. Resolve each participant to a `person` entity: search `kg/entities/person/` by title and aliases; create a minimal entity when missing.
   3. Identify companies, projects and topics that the conversation is about; resolve or create their entities the same way.
   4. Write or update `kg/entities/meeting/<date>-<platform>-<native_meeting_id>.md` with the frontmatter and the sections `CLAUDE.md` prescribes. If the file already exists, update it in place; do not create a second one.
   5. File every decision as `kg/entities/decision/<date>-<short-slug>.md` (frontmatter `type: decision`, `id`, `title`, `aliases`, `date`, `meeting: [[<meeting title>]]`, `status: decided`), and link it from the meeting entity.
   6. Add dated, attributed facts to the person, company, project and topic entities you touched (what changed, what was said about them, open items involving them).
   7. Update the `index.md` of every type folder you added to or renamed in, and `README.md` counts.
   8. Append one `log.md` entry under today's date naming the meeting and the entities touched.
   9. Move the transcript to `uploads/processed/` with the same filename.
3. Reply with one line per transcript processed: the meeting title and the number of entities created and updated.

Never run git commands; the platform commits your changes. Never touch `Dashboards/`.
