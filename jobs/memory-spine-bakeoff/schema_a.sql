-- Contender A — pgvector-hybrid (incumbent), isolated in mimir_bakeoff.
-- bakeoff.hybrid_search is a FAITHFUL copy of production search.hybrid_search:
--   text  = ts_rank_cd(english tsvector) + pg_trgm similarity
--   vector= cosine (1 - <=>) over nomic-768 embeddings
--   combined = 0.4*text + 0.6*vector   (the incumbent's 40/60 split)
-- Only difference vs prod: no source_type filter, no retrieval-stat side effect
-- (read-only bake-off). Same scoring maths, so contender A == the incumbent.

CREATE SCHEMA IF NOT EXISTS bakeoff;

DROP TABLE IF EXISTS bakeoff.chunks CASCADE;
CREATE TABLE bakeoff.chunks (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id   text    NOT NULL,           -- relative doc path in the snapshot
    chunk_index int     NOT NULL,
    title       text,
    content     text    NOT NULL,
    embedding   vector(768),
    metadata    jsonb   NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_id, chunk_index)
);

CREATE INDEX chunks_hnsw   ON bakeoff.chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_trgm   ON bakeoff.chunks USING gin (content gin_trgm_ops);
CREATE INDEX chunks_tsv    ON bakeoff.chunks USING gin (to_tsvector('english', content));

CREATE OR REPLACE FUNCTION bakeoff.hybrid_search(
    p_query  text,
    p_limit  integer DEFAULT 5,
    p_vector vector  DEFAULT NULL
)
RETURNS TABLE(
    source_id text, chunk_index integer, title text, content text,
    text_score double precision, vector_score double precision, combined_score double precision
)
LANGUAGE plpgsql STABLE AS $fn$
BEGIN
    RETURN QUERY
    WITH text_matches AS (
        SELECT e.id,
               ts_rank_cd(to_tsvector('english', e.content), plainto_tsquery('english', p_query)) AS t_rank,
               similarity(e.content, p_query) AS trgm_score
        FROM bakeoff.chunks e
        WHERE to_tsvector('english', e.content) @@ plainto_tsquery('english', p_query)
           OR similarity(e.content, p_query) > 0.03
           OR e.title ILIKE '%' || p_query || '%'
    ),
    vector_matches AS (
        SELECT e.id, 1 - (e.embedding <=> p_vector) AS v_rank
        FROM bakeoff.chunks e
        WHERE p_vector IS NOT NULL AND e.embedding IS NOT NULL
        ORDER BY e.embedding <=> p_vector
        LIMIT p_limit * 3
    ),
    scored AS (
        SELECT e.id,
               COALESCE(tm.t_rank + tm.trgm_score, 0) AS text_score,
               COALESCE(vm.v_rank, 0) AS vec_score,
               (0.4 * COALESCE(tm.t_rank + tm.trgm_score, 0))
                 + (0.6 * COALESCE(vm.v_rank, 0)) AS combined
        FROM bakeoff.chunks e
        LEFT JOIN text_matches   tm ON tm.id = e.id
        LEFT JOIN vector_matches vm ON vm.id = e.id
        WHERE tm.id IS NOT NULL OR vm.id IS NOT NULL
    )
    SELECT e.source_id, e.chunk_index, e.title, e.content,
           s.text_score::float, s.vec_score::float, s.combined::float
    FROM scored s
    JOIN bakeoff.chunks e ON e.id = s.id
    ORDER BY s.combined DESC
    LIMIT p_limit;
END;
$fn$;
