"""Durable outbound notification ledger and channel configuration."""

from alembic import op

revision = "0062_notifications"
down_revision = "0061_part_combines"
branch_labels = depends_on = None


def upgrade():
    op.execute(
        """
CREATE TABLE notification_policy (
    id SERIAL NOT NULL,
    member_events JSONB NOT NULL,
    PRIMARY KEY (id)
)

"""
    )
    op.execute(
        """
CREATE TABLE notification_channels (
    owner_id UUID,
    name VARCHAR(120) NOT NULL,
    kind VARCHAR(30) NOT NULL,
    encrypted_secrets TEXT NOT NULL,
    events JSONB NOT NULL,
    enabled BOOLEAN NOT NULL,
    digest_minutes INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE
)

"""
    )
    op.execute(
        """
CREATE TABLE notification_events (
    key VARCHAR(300) NOT NULL,
    event_type VARCHAR(60) NOT NULL,
    owner_id UUID,
    subject_id UUID,
    payload JSONB NOT NULL,
    routed_at TIMESTAMP WITH TIME ZONE,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (key),
    FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE
)

"""
    )
    op.execute("CREATE INDEX ix_notification_events_routed_at ON notification_events (routed_at)")
    op.execute(
        """
CREATE TABLE notification_deliveries (
    channel_id UUID NOT NULL,
    event_id UUID NOT NULL,
    generation INTEGER NOT NULL,
    state VARCHAR(20) NOT NULL,
    due_at TIMESTAMP WITH TIME ZONE NOT NULL,
    attempted_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    message VARCHAR(300),
    batch_id UUID,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (channel_id, event_id),
    FOREIGN KEY(channel_id) REFERENCES notification_channels (id) ON DELETE CASCADE,
    FOREIGN KEY(event_id) REFERENCES notification_events (id) ON DELETE CASCADE
)

"""
    )
    op.execute("CREATE INDEX ix_notification_deliveries_due_at ON notification_deliveries (due_at)")
    op.execute("""
        CREATE TABLE notification_request_states (
            intent_id UUID PRIMARY KEY REFERENCES acquisition_intents(id) ON DELETE CASCADE,
            state VARCHAR(20) NOT NULL,
            revision INTEGER NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO notification_request_states(intent_id,state,revision)
        SELECT intent_id, CASE WHEN bool_or(approval_status='approved') THEN 'approved'
          WHEN bool_or(approval_status='pending') THEN 'pending' ELSE 'declined' END, 0
        FROM acquisition_reasons WHERE active GROUP BY intent_id
    """)
    op.execute(CAPTURE_FUNCTION)
    for table in CAPTURE_TABLES:
        op.execute(
            f"CREATE CONSTRAINT TRIGGER notification_capture AFTER INSERT OR UPDATE "
            f"ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION capture_notification_event()"
        )


def downgrade():
    for table in CAPTURE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS notification_capture ON {table}")
    op.execute("DROP FUNCTION capture_notification_event()")
    op.execute("DROP TABLE IF EXISTS notification_request_states")
    op.drop_table("notification_deliveries")
    op.drop_table("notification_events")
    op.drop_table("notification_channels")
    op.drop_table("notification_policy")


CAPTURE_TABLES = (
    "acquisition_reasons",
    "download_attempts",
    "import_entries",
    "operations",
    "integrations",
    "source_connections",
    "series_gap_sightings",
    "list_entries",
    "download_fulfillments",
    "download_memberships",
)


CAPTURE_FUNCTION = r"""
CREATE FUNCTION capture_notification_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
 row_data jsonb := to_jsonb(NEW);
 old_data jsonb := CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(OLD) ELSE '{}'::jsonb END;
 kind text;
 recipient uuid;
 subject uuid;
 link text;
 summary text;
 event_key text;
 book_id uuid;
 book_cover text;
 book_title text;
 request_state text;
 request_revision integer;
BEGIN
 -- Deferred constraint triggers read the final row, not an intermediate flush.
 -- Imports, for example, start held before their destination is assigned.
 IF TG_TABLE_NAME = 'source_connections' THEN
   EXECUTE format('SELECT * FROM %I WHERE key=$1', TG_TABLE_NAME) INTO NEW USING NEW.key;
 ELSIF TG_TABLE_NAME='download_memberships' THEN
   EXECUTE 'SELECT * FROM download_memberships WHERE selection_id=$1'
   INTO NEW USING NEW.selection_id;
 ELSE
   EXECUTE format('SELECT * FROM %I WHERE id=$1', TG_TABLE_NAME) INTO NEW USING NEW.id;
 END IF;
 row_data := to_jsonb(NEW);
 IF coalesce(row_data->>'id', row_data->>'key', row_data->>'selection_id') IS NULL
   THEN RETURN NEW; END IF;
 IF TG_TABLE_NAME = 'acquisition_reasons' THEN
   IF (row_data->>'approval_status' IS NOT DISTINCT FROM old_data->>'approval_status'
       AND row_data->>'active' IS NOT DISTINCT FROM old_data->>'active') THEN RETURN NEW; END IF;
   SELECT owner_id, work_id INTO recipient, book_id
   FROM acquisition_intents WHERE id=NEW.intent_id;
   subject := NEW.intent_id;
   SELECT CASE WHEN count(*)=0 THEN 'inactive'
      WHEN bool_or(approval_status='approved') THEN 'approved'
      WHEN bool_or(approval_status='pending') THEN 'pending' ELSE 'declined' END
   INTO request_state FROM acquisition_reasons WHERE intent_id=NEW.intent_id AND active;
   INSERT INTO notification_request_states(intent_id,state,revision)
   VALUES(NEW.intent_id,request_state,1)
   ON CONFLICT(intent_id) DO UPDATE SET state=EXCLUDED.state,
     revision=notification_request_states.revision+1
   WHERE notification_request_states.state IS DISTINCT FROM EXCLUDED.state
   RETURNING revision INTO request_revision;
   IF request_revision IS NULL OR request_state='inactive' THEN RETURN NEW; END IF;
   kind := 'request.' || request_state;
   link := '/requests';
   summary := CASE request_state WHEN 'pending' THEN 'A book request needs approval'
     WHEN 'approved' THEN 'Your book request was approved'
     ELSE 'Your book request was declined' END;
   event_key := kind || ':' || subject::text || ':' || request_revision::text;
 ELSIF TG_TABLE_NAME = 'download_attempts' THEN
   IF NEW.state IS NOT DISTINCT FROM old_data->>'state' THEN RETURN NEW; END IF;
   SELECT i.owner_id, i.work_id INTO recipient, book_id
   FROM acquisition_selections s JOIN acquisition_intents i ON i.id=s.intent_id
   WHERE s.id=NEW.selection_id;
   subject := NEW.id; link := '/requests#downloads';
   IF NEW.state IN ('downloading','complete') THEN kind := 'download.started';
     summary := 'Your download has started';
   ELSIF NEW.state IN ('held','uncertain') THEN kind := 'operation.held'; summary := NEW.message;
   ELSE RETURN NEW; END IF;
 ELSIF TG_TABLE_NAME = 'download_memberships' THEN
   IF TG_OP != 'INSERT' OR NOT EXISTS(SELECT 1 FROM download_attempts
     WHERE id=NEW.attempt_id AND state IN ('downloading','complete')) THEN RETURN NEW; END IF;
   SELECT i.owner_id, i.work_id INTO recipient, book_id
   FROM acquisition_selections s JOIN acquisition_intents i ON i.id=s.intent_id
   WHERE s.id=NEW.selection_id;
   kind := 'download.started'; subject := NEW.attempt_id; link := '/requests#downloads';
   summary := 'Your request joined an existing download';
 ELSIF TG_TABLE_NAME = 'import_entries' THEN
   IF NEW.state IS NOT DISTINCT FROM old_data->>'state' THEN RETURN NEW; END IF;
   SELECT r.owner_id, '/organization/inspections?inspection=' || p.inspection_id::text
   INTO recipient, link FROM import_runs r JOIN frozen_import_plans p ON p.id=r.plan_id
   WHERE r.id=NEW.run_id;
   subject := NEW.id;
   SELECT work_id INTO book_id FROM versions WHERE id=NEW.version_id;
   IF NEW.state='confirmed' THEN kind := 'import.available';
     summary := 'Your book is confirmed available in the library';
     event_key := 'available:' || recipient::text || ':' || NEW.asset_id::text;
     link := '/library';
   ELSIF NEW.state IN ('held','cancel-held') THEN kind := 'operation.held'; summary := NEW.message;
   ELSE RETURN NEW; END IF;
 ELSIF TG_TABLE_NAME = 'download_fulfillments' THEN
   IF TG_OP != 'INSERT' THEN RETURN NEW; END IF;
   SELECT i.owner_id, i.work_id INTO recipient, book_id FROM acquisition_targets t
   JOIN acquisition_intents i ON i.id=t.intent_id WHERE t.id=NEW.target_id;
   kind := 'import.available'; subject := NEW.id; link := '/library';
   summary := 'Your requested book is confirmed available in your library';
   event_key := 'available:' || recipient::text || ':' || NEW.asset_id::text;
 ELSIF TG_TABLE_NAME = 'operations' THEN
   IF NEW.status IS NOT DISTINCT FROM old_data->>'status' OR NEW.kind LIKE 'recovery.%'
      OR NEW.kind IN ('organization.publish', 'acquisition.download')
      OR (NEW.status='held' AND (NEW.payload ? 'waiting_for_release'
          OR NEW.kind='lists.release-wait'))
      THEN RETURN NEW; END IF;
   IF NEW.status='failed' THEN kind := 'operation.failed';
   ELSIF NEW.status IN ('held','needs-review') THEN kind := 'operation.held';
   ELSE RETURN NEW; END IF;
   recipient := NEW.owner_id; subject := NEW.id; link := '/settings#logs'; summary := NEW.message;
 ELSIF TG_TABLE_NAME IN ('integrations','source_connections') THEN
   IF row_data->>'status' IS NOT DISTINCT FROM old_data->>'status'
      OR row_data->>'status' NOT IN ('error','failed','unavailable','expired','unreachable',
        'unauthorized','forbidden','network','authentication','permission','timeout','route')
      THEN RETURN NEW; END IF;
   kind := 'connection.problem'; link := '/settings#libraries';
   recipient := (row_data->>'owner_id')::uuid; subject := (row_data->>'id')::uuid;
   summary := 'A configured connection needs attention. Open settings for details.';
   event_key := TG_TABLE_NAME || ':' || coalesce(row_data->>'id',row_data->>'key')
     || ':' || kind || ':' || coalesce(row_data->>'last_success_at','initial');
 ELSIF TG_TABLE_NAME = 'series_gap_sightings' THEN
   IF TG_OP != 'INSERT' THEN RETURN NEW; END IF;
   kind := 'discovery.gap'; recipient := NEW.user_id; subject := NEW.work_id;
   book_id := NEW.work_id;
   link := '/discover/series'; summary := 'A new missing book was found in a series you own';
   event_key := kind || ':' || recipient::text || ':' || NEW.external_id || ':' || subject::text;
 ELSIF TG_TABLE_NAME = 'list_entries' THEN
   IF TG_OP != 'INSERT' OR NEW.locally_added OR NOT EXISTS
     (SELECT 1 FROM list_subscriptions subscription WHERE list_id=NEW.list_id
       AND baseline_at < NEW.created_at
       AND coalesce(to_jsonb(subscription)->>'source_kind','') NOT IN ('author','series'))
     THEN RETURN NEW; END IF;
   SELECT owner_id INTO recipient FROM book_lists WHERE id=NEW.list_id;
   kind := 'discovery.list'; subject := NEW.work_id;
   book_id := NEW.work_id;
   link := '/lists/' || NEW.list_id::text; summary := 'A followed list has a new book';
   event_key := kind || ':' || NEW.list_id::text || ':' || subject::text;
 END IF;
 IF kind IS NULL THEN RETURN NEW; END IF;
 SELECT title, cover_url INTO book_title, book_cover FROM works WHERE id=book_id;
 IF book_cover !~ ('^https://(assets[.]hardcover[.]app|images-na[.]ssl-images-amazon[.]com|'
   || 'images[.]gr-assets[.]com)/[^? #@]+$') THEN book_cover := NULL; END IF;
 summary := coalesce(book_title || ': ', '') || summary;
 summary := regexp_replace(summary, '[a-zA-Z][a-zA-Z0-9+.-]*://[^[:space:]]+',
   '[private URL]', 'g');
 summary := regexp_replace(summary,
   '(token|password|secret|cookie|authorization|api[_-]?key)[[:space:]]*[:=].*',
   '[private detail]', 'i');

 IF kind='download.started' THEN
   event_key := kind || ':' || subject::text || ':' || recipient::text;
   IF TG_TABLE_NAME='download_attempts' THEN
     INSERT INTO notification_events(id,key,event_type,owner_id,subject_id,payload)
     SELECT gen_random_uuid(), kind || ':' || subject::text || ':' || i.owner_id::text,
       kind,i.owner_id,subject,jsonb_build_object('title','Download started',
         'message',summary,'path',link,'cover_url',book_cover)
     FROM download_memberships m JOIN acquisition_selections s ON s.id=m.selection_id
     JOIN acquisition_intents i ON i.id=s.intent_id WHERE m.attempt_id=subject
     ON CONFLICT(key) DO NOTHING;
   END IF;
 END IF;
 INSERT INTO notification_events(id,key,event_type,owner_id,subject_id,payload)
 VALUES(gen_random_uuid(),coalesce(event_key,TG_TABLE_NAME || ':' || subject::text || ':' || kind),
 kind,recipient,subject,jsonb_build_object(
   'title','Dewarr notification','message',summary,'path',link,'cover_url',book_cover))
 ON CONFLICT(key) DO NOTHING;
 RETURN NEW;
END $$
"""
