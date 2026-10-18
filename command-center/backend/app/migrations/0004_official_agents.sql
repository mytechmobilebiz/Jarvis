-- 0.3.0: the seven official agents replace the earlier agent modules.
-- Records keep their history; only the agent names change:
--   wordpress_monitor        -> monitoring
--   sales, support           -> communications
--   seo, content (blog)      -> seo_content
--   ads, content (social)    -> social_marketing

CREATE FUNCTION pg_temp.cc_new_agent(old TEXT, cap TEXT, destination TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN old = 'wordpress_monitor' THEN 'monitoring'
        WHEN old IN ('sales', 'support') THEN 'communications'
        WHEN old = 'seo' THEN 'seo_content'
        WHEN old = 'ads' THEN 'social_marketing'
        WHEN old = 'content' AND cap = 'draft_social_posts' THEN 'social_marketing'
        WHEN old = 'content' AND cap = 'publish_draft' AND coalesce(destination, '') <> 'wordpress' THEN 'social_marketing'
        WHEN old = 'content' THEN 'seo_content'
        ELSE old
    END
$$;

UPDATE tasks SET agent = pg_temp.cc_new_agent(agent, capability, params->>'destination')
 WHERE agent IN ('wordpress_monitor', 'sales', 'support', 'seo', 'ads', 'content');

UPDATE approvals a SET agent = t.agent
  FROM tasks t
 WHERE a.task_id = t.id AND a.agent IN ('wordpress_monitor', 'sales', 'support', 'seo', 'ads', 'content');

UPDATE schedules SET agent = pg_temp.cc_new_agent(agent, capability, params->>'destination')
 WHERE agent IN ('wordpress_monitor', 'sales', 'support', 'seo', 'ads', 'content');

UPDATE artifacts SET agent = CASE WHEN kind = 'social_drafts' THEN 'social_marketing'
                                  ELSE pg_temp.cc_new_agent(agent, NULL, 'wordpress') END
 WHERE agent IN ('wordpress_monitor', 'sales', 'support', 'seo', 'ads', 'content');

UPDATE outbox SET agent = CASE WHEN integration = 'social' THEN 'social_marketing'
                               ELSE pg_temp.cc_new_agent(agent, NULL, 'wordpress') END
 WHERE agent IN ('wordpress_monitor', 'sales', 'support', 'seo', 'ads', 'content');

UPDATE events SET source = pg_temp.cc_new_agent(source, NULL, 'wordpress')
 WHERE source IN ('wordpress_monitor', 'sales', 'support', 'seo', 'ads', 'content');

-- An agent switched off before the upgrade stays off: a new agent is enabled
-- only if every old agent folded into it was enabled.
INSERT INTO agent_state (name, enabled, updated_by)
SELECT new_name, bool_and(enabled), 'upgrade to 0.3.0'
  FROM (SELECT pg_temp.cc_new_agent(name, NULL, 'wordpress') AS new_name, enabled
          FROM agent_state WHERE name IN ('wordpress_monitor', 'sales', 'support', 'seo', 'ads', 'content')
        UNION ALL
        SELECT 'social_marketing', enabled FROM agent_state WHERE name = 'content') AS folded
 GROUP BY new_name
ON CONFLICT (name) DO UPDATE SET enabled = agent_state.enabled AND excluded.enabled, updated_by = excluded.updated_by, updated_at = now();

DELETE FROM agent_state WHERE name IN ('wordpress_monitor', 'sales', 'support', 'seo', 'ads', 'content');

DROP FUNCTION pg_temp.cc_new_agent(TEXT, TEXT, TEXT);
