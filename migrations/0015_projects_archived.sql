-- archived_at: when the user archived the project; NULL = not archived. For a site that went live and whose plan has
-- since stopped: it can't go to Trash (services/projects), so archiving is how it leaves the project list. Like Trash,
-- an archived project drops out of the list and can't be opened until it's taken back out -- name, pin and folder all
-- kept. Archiving only tidies the list: it doesn't touch the production site or anything bought.

ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
