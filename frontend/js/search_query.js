/* Field-scoped search for the Library — e621/GitHub-style `field:value` terms.
 *
 * The shelf search was a substring match on title + name. That is fine for
 * "find the piece called Blep" and useless for "white tiger pieces by Inkwolf
 * that aren't posted anywhere yet" — which is the question you actually have
 * once the catalogue passes a hundred works and every piece carries 30 tags.
 *
 * Grammar (all terms AND together, like e621):
 *
 *   tiger                 bare words → substring of title or name
 *   tag:white_tiger       has that tag
 *   -tag:cum              does NOT have it
 *   tag_exclude:cum       same thing, spelled out
 *   tag:tiger,lynx        OR within one field — tiger or lynx
 *   tag:cum*              trailing wildcard
 *   artist:"Dan Crescent" quoted value, spaces kept
 *   platform:fa           posted there
 *   status:draft          not posted anywhere yet
 *   rating:explicit  type:artwork  persona:Main  series:"Sample Series"
 *
 * Design notes worth keeping:
 *
 *   - Unknown fields fall back to TEXT rather than erroring or matching
 *     nothing. `colour:blue` should find a piece whose title says "colour:blue"
 *     rather than silently returning an empty shelf — a search box that
 *     mysteriously goes blank teaches you to distrust it.
 *   - `-` and `_exclude` are the same operator. `-tag:` is what e621 users type;
 *     `tag_exclude:` is what you reach for when you're not sure `-` is
 *     supported. Supporting one and not the other is a coin flip the user loses.
 *   - Values are matched case-insensitively everywhere. Tags are lowercase by
 *     convention but nothing enforces it, and `artist:inkwolf` should find
 *     "Inkwolf".
 *   - An unterminated quote is treated as running to the end of the input,
 *     because you are searching WHILE typing — `artist:"Dan` must not blank the
 *     shelf between the opening quote and the closing one.
 */
(function () {
    'use strict';

    // field → how to pull the comparable strings off a work, and how to compare.
    //   list  = match any element exactly (tags, platforms)
    //   text  = substring match against any element
    //   exact = whole-string match (ratings, types — a small closed set)
    const FIELDS = {
        tag:      { get: w => w.tags || [],                        mode: 'list' },
        platform: { get: w => w.platforms || [],                   mode: 'list' },
        artist:   { get: w => [w.artist_name || ''],               mode: 'text' },
        persona:  { get: w => w.persona_names || [],               mode: 'text' },
        series:   { get: w => [w.series || ''],                    mode: 'text' },
        title:    { get: w => [w.title || '', w.name || ''],       mode: 'text' },
        rating:   { get: w => [w.rating || ''],                    mode: 'exact' },
        type:     { get: w => [w.content_type || ''],              mode: 'exact' },
    };

    // `status:` is not a field on the work — it is a question about several.
    const STATUS = {
        posted:       w => (w.publication_count || 0) > 0,
        draft:        w => (w.publication_count || 0) === 0,
        drafts:       w => (w.publication_count || 0) === 0,
        unposted:     w => (w.publication_count || 0) === 0,
        junk:         w => !!w.is_junk,
        unattributed: w => w.content_type !== 'story' && !!w.needs_artist,
        attributed:   w => w.content_type !== 'story' && !w.needs_artist,
    };

    const FIELD_NAMES = Object.keys(FIELDS).concat(['status']);

    /* Split on whitespace but keep quoted runs together. An unclosed quote runs
     * to the end of the string — see the note above about searching mid-type. */
    function tokenise(input) {
        const out = [];
        let buf = '', quote = '';
        for (const ch of String(input || '')) {
            if (quote) {
                if (ch === quote) quote = '';
                else buf += ch;
            } else if (ch === '"' || ch === "'") {
                quote = ch;
            } else if (/\s/.test(ch)) {
                if (buf) { out.push(buf); buf = ''; }
            } else {
                buf += ch;
            }
        }
        if (buf) out.push(buf);
        return out;
    }

    function valueMatches(candidate, want, mode) {
        const c = String(candidate || '').toLowerCase();
        if (want.indexOf('*') !== -1) {
            // Wildcards only make sense as a pattern; escape everything else so a
            // tag like `oral_(sex)` can't blow up as a regex.
            const rx = new RegExp('^' + want.split('*')
                .map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
                .join('.*') + '$');
            return rx.test(c);
        }
        if (mode === 'text') return c.indexOf(want) !== -1;
        return c === want;
    }

    /** parse(input) → {terms:[{field,values,negate,mode}], text:[...]} */
    function parse(input) {
        const terms = [];
        const text = [];
        for (const raw of tokenise(input)) {
            let tok = raw;
            let negate = false;
            if (tok[0] === '-' && tok.length > 1) { negate = true; tok = tok.slice(1); }

            const colon = tok.indexOf(':');
            if (colon <= 0) { text.push(tok.toLowerCase()); continue; }

            let field = tok.slice(0, colon).toLowerCase();
            const value = tok.slice(colon + 1);
            if (!value) { text.push(tok.toLowerCase()); continue; }

            if (field.endsWith('_exclude')) { negate = true; field = field.slice(0, -8); }
            // `-` and `_exclude` are the same operator; using both is not an error,
            // it just reads as one negation rather than cancelling out. `--tag:` is
            // a typo, not a double negative, and treating it as one would be a
            // hostile reading of an obvious mistake.

            if (field !== 'status' && !FIELDS[field]) {
                // Unknown field — treat the whole token as literal text.
                text.push(raw.toLowerCase());
                continue;
            }
            terms.push({
                field,
                negate,
                values: value.toLowerCase().split(',').map(v => v.trim()).filter(Boolean),
                mode: field === 'status' ? 'status' : FIELDS[field].mode,
            });
        }
        return { terms, text };
    }

    function termMatches(work, term) {
        let hit;
        if (term.field === 'status') {
            hit = term.values.some(v => (STATUS[v] || (() => false))(work));
        } else {
            const have = FIELDS[term.field].get(work) || [];
            hit = term.values.some(v => have.some(c => valueMatches(c, v, term.mode)));
        }
        return term.negate ? !hit : hit;
    }

    function match(work, parsed) {
        for (const t of parsed.text) {
            const hay = ((work.title || '') + ' ' + (work.name || '')).toLowerCase();
            if (hay.indexOf(t) === -1) return false;
        }
        return parsed.terms.every(t => termMatches(work, t));
    }

    /** True when the query explicitly asks to SEE junk, so the Library's
     *  hide-by-default rule can stand aside for `status:junk`. */
    function wantsJunk(parsed) {
        return parsed.terms.some(t =>
            t.field === 'status' && !t.negate && t.values.indexOf('junk') !== -1);
    }

    function filter(list, input) {
        const parsed = parse(input);
        if (!parsed.terms.length && !parsed.text.length) return list;
        return list.filter(w => match(w, parsed));
    }

    const api = { parse, match, filter, wantsJunk, FIELDS, STATUS, FIELD_NAMES, tokenise };
    if (typeof window !== 'undefined') window.SearchQuery = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
