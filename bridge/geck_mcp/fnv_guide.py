"""fnv_guide: which tool for which problem. Guides (Markdown in geck_mcp/guides/), a search over the tool
descriptions, and a search over saved engine research notes and the recorded engine facts."""
import os
import re
from pathlib import Path

from .fnv_conv import ConvError, dispatch, page, verdict

GUIDES_DIR = Path(__file__).resolve().parent / 'guides'
STOP = set('a an the to of for in on is it my i do does how what which why when and or with that this tool tools '
           'use find get can not no be are was'.split())
TOOL_NAME = re.compile(r'\b(?:fnv|geck)_[a-z0-9_]+\b')


def stems(text):
    """Lower-case words of a text with the commonest endings removed, stop words dropped."""
    out = []
    for w in re.findall(r'[a-z0-9]+', text.lower()):
        if w in STOP:
            continue
        for suffix in ('ing', 'ed', 'es', 's'):
            if len(w) > len(suffix) + 2 and w.endswith(suffix):
                w = w[:-len(suffix)]
                break
        out.append(w)
    return out


def load_guides(folder=GUIDES_DIR):
    """{name: {name, title, summary, words, text}}. A guide starts with '# Title', then 'Summary: ...' and
    'Words: a, b, c' lines."""
    guides = {}
    for path in sorted(Path(folder).glob('*.md')):
        text = path.read_text(encoding='utf-8')
        title = re.search(r'^# (.+)$', text, re.M)
        summary = re.search(r'^Summary: (.+)$', text, re.M)
        words = re.search(r'^Words: (.+)$', text, re.M)
        guides[path.stem] = {'name': path.stem, 'title': title.group(1).strip() if title else path.stem,
                             'summary': summary.group(1).strip() if summary else '',
                             'words': words.group(1).strip() if words else '', 'text': text}
    return guides


def tool_table(toolsets=None):
    """{name: {kind, toolset, description}} for every offline and live fnv_* tool and the legacy tools, and the
    geck_* tools when the editor toolset is off. A tool whose toolset is not in `toolsets` (default: every one but
    legacy) is not registered, so its description says how to turn it on (TOOL-MERGE step 1)."""
    from .fnv_live_tools import FNV_LIVE_TOOLS
    from .fnv_tools import FNV_TOOLS
    from .server import LEGACY_TOOLS
    from .service import CAPABILITIES
    from .toolsets import ALWAYS, DEFAULT_TOOLSETS, hidden_note, toolset_of
    active = set(DEFAULT_TOOLSETS if toolsets is None else toolsets)
    table = {n: {'kind': 'read', 'description': d} for n, d in FNV_TOOLS.items()}
    table.update({n: {'kind': k, 'description': d} for n, (k, d) in FNV_LIVE_TOOLS.items()})
    effects = CAPABILITIES.get('tools') or {}
    geck = [n for n in effects if n.startswith('geck_') and n not in LEGACY_TOOLS] if 'editor' not in active else []
    for name in [*LEGACY_TOOLS, *geck]:
        effect = (effects.get(name) or {}).get('effect') or name.replace('_', ' ')
        table[name] = {'kind': 'control', 'description': effect}
    for name, row in table.items():
        row['toolset'] = toolset_of(name)
        if row['toolset'] not in active and name not in ALWAYS:
            row['description'] = '%s. %s' % (row['description'], hidden_note(row['toolset']))
    return table


def score_guide(query, guide):
    title = set(stems(guide['title'] + ' ' + guide['name'].replace('-', ' ')))
    words = set(stems(guide['words']))
    summary = set(stems(guide['summary']))
    body = stems(guide['text'])
    total = 0
    for q in set(query):
        total += 4 * (q in title) + 4 * (q in words) + 2 * (q in summary) + min(body.count(q), 5) * 0.2
    return total


class GuideTools:
    def __init__(self, research_dir=None, guides_dir=GUIDES_DIR, toolsets=None):
        self.research_dir = research_dir or os.environ.get('FNV_RESEARCH_DIR')
        self.guides_dir = guides_dir
        self.toolsets = toolsets          # the server's active toolsets (None: the default ones)

    # ------------------------------------------------------------ list, topic, find
    def list(self):
        guides = load_guides(self.guides_dir)
        rows = [{'name': g['name'], 'title': g['title'], 'summary': g['summary']} for g in guides.values()]
        return verdict('%d guides' % len(rows), 'Call fnv_guide topic with a name or words that describe the problem.',
                       rows=rows)

    def topic(self, words):
        if not words or not words.strip():
            raise ValueError('topic needs words: a guide name or a description of the problem')
        guides = load_guides(self.guides_dir)
        key = words.strip().lower().replace(' ', '-')
        ranked = []
        if key in guides:
            ranked = [(100, guides[key])]
        else:
            query = stems(words)
            ranked = sorted(((score_guide(query, g), g) for g in guides.values()), key=lambda x: -x[0])
            ranked = [x for x in ranked if x[0] >= 1]
        if not ranked:
            return verdict('No guide matches %r' % words, 'Call fnv_guide find to search the tools, or list for all guides.',
                           guides=[g['name'] for g in guides.values()])
        best = ranked[0][1]
        return verdict('Guide: %s (%s)' % (best['title'], best['name']),
                       'Best match for %r.' % words if key not in guides else 'Exact name.',
                       otherCandidates=[{'name': g['name'], 'title': g['title'], 'score': round(s, 1)}
                                        for s, g in ranked[1:4]], guide=best['text'])

    def find(self, words, limit=20, offset=0, brief=False):
        if not words or not words.strip():
            raise ValueError('find needs words, for example "script does not run" or "crash log"')
        query = stems(words)
        rows = []
        for name, t in tool_table(self.toolsets).items():
            name_words, description = set(stems(name.replace('_', ' '))), set(stems(t['description']))
            score = sum(3 * (q in name_words) + 1 * (q in description) for q in set(query))
            if score:
                rows.append({'kind': 'tool', 'name': name, 'toolKind': t['kind'], 'toolset': t['toolset'],
                             'score': score, 'description': t['description']})
        for g in load_guides(self.guides_dir).values():
            score = score_guide(query, g)
            if score >= 1:
                rows.append({'kind': 'guide', 'name': g['name'], 'score': round(score, 1), 'description': g['summary']})
        rows.sort(key=lambda r: (-r['score'], r['name']))
        head = ('%d matches for %r, best: %s' % (len(rows), words, rows[0]['name'])) if rows else 'No match for %r' % words
        return verdict(head, None if rows else 'Try other words, or call fnv_guide list.',
                       **page(rows, limit, offset, brief=brief, brief_fields=['kind', 'name', 'description']))

    # ------------------------------------------------------------ engine research and recorded facts
    def sections(self, folder):
        """Every heading section of every top-level Markdown file: (file, line, heading path, lines)."""
        out = []
        for path in sorted(Path(folder).glob('*.md')):
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            stack, start, begin = [], None, 0
            for i, line in enumerate(lines + ['# end']):
                match = re.match(r'^(#{1,6})\s+(.*\S)', line)
                if not match:
                    continue
                if start is not None:
                    out.append((path.name, start + 1, ' > '.join(h for _, h in stack), lines[begin:i]))
                level = len(match.group(1))
                stack = [s for s in stack if s[0] < level] + [(level, match.group(2).strip())]
                start, begin = i, i + 1
        return out

    @staticmethod
    def excerpt(lines, query):
        best, best_hits = '', 0
        for line in lines:
            low = line.lower()
            hits = sum(1 for q in set(query) if q in low)
            if hits > best_hits:
                best, best_hits = line.strip(), hits
        if not best:
            best = next((l.strip() for l in lines if l.strip()), '')
        return best if len(best) <= 240 else best[:237] + '...'

    def engine(self, words, limit=20, offset=0, brief=False):
        if not words or not words.strip():
            raise ValueError('engine needs words, for example "barter price" or "fast travel"')
        query = stems(words)
        facts = []
        from .fnv_facts import FACTS
        for name, fact in FACTS.items():
            haystack = ' '.join([name.replace('.', ' '), str(fact.get('evidence', ''))]).lower()
            hits = sum(1 for q in set(query) if q in haystack)
            if hits:
                shown = {k: (hex(v) if isinstance(v, int) and k in ('address', 'offset') else v) for k, v in fact.items()}
                facts.append({'name': name, 'hits': hits, **shown})
        facts.sort(key=lambda f: -f['hits'])
        folder = self.research_dir
        notes = []
        # With no folder of the user's own notes, the guides that ship with the server are searched (release audit L8)
        searched = folder if folder and Path(folder).is_dir() else (self.guides_dir if not folder else None)
        if searched and Path(searched).is_dir():
            n = len(set(query))
            need = n if n <= 2 else max(2, (n + 1) // 2)
            source = 'research' if searched == folder else 'guide'
            for file, line, heading, body in self.sections(searched):
                text = (heading + '\n' + '\n'.join(body)).lower()
                head = heading.lower()
                present = [q for q in set(query) if q in text]
                if len(present) >= need:
                    score = len(present) * 10 + 5 * sum(1 for q in present if q in head) + min(sum(text.count(q) for q in present), 20) / 10
                    notes.append({'file': file, 'line': line, 'heading': heading, 'score': round(score, 1),
                                  'source': source, 'excerpt': self.excerpt(body, query)})
            notes.sort(key=lambda n: -n['score'])
        reason, nxt = None, None
        if not folder:
            reason = ('No research folder is set: the shipped guides and the recorded facts were searched. The decomp '
                      'itself is searched with fnv_func.')
            nxt = ('For how the engine does it, call fnv_func with the same words (mode="code" searches the '
                   'decomp source). To search notes of your own too, set fnv.research_dir in the project file, or '
                   'the environment variable FNV_RESEARCH_DIR.')
        elif not Path(folder).is_dir():
            reason, nxt = 'The research folder %s does not exist.' % folder, 'Fix fnv.research_dir or FNV_RESEARCH_DIR.'
        head = '%d %s sections and %d recorded facts match %r' % (
            len(notes), 'research' if folder else 'guide', len(facts), words)
        if not notes and not facts and not nxt:
            nxt = 'Try other words, or read the decomp with fnv_func.'
        out = verdict(head, reason, **page(notes, limit, offset, brief=brief, brief_fields=['file', 'line', 'heading']))
        out['facts'] = facts[:10]
        out['researchDir'] = str(folder) if folder else None
        if nxt:
            out['next'] = nxt
        return out

    def run(self, action, words=None, limit=20, offset=0, brief=False):
        return dispatch(action, {
            'list': self.list, 'topic': lambda: self.topic(words),
            'find': lambda: self.find(words, limit, offset, brief),
            'engine': lambda: self.engine(words, limit, offset, brief)})


def guide_names(folder=GUIDES_DIR):
    return [p.stem for p in sorted(Path(folder).glob('*.md'))]


def guide_text(name, folder=GUIDES_DIR):
    path = Path(folder) / (name + '.md')
    if not re.match(r'^[a-z0-9-]+$', name) or not path.exists():
        raise ConvError('NOT_FOUND', 'No guide %r. Guides: %s.' % (name, ', '.join(guide_names(folder))))
    return path.read_text(encoding='utf-8')
