"""Exact, auditable bilingual note rules. No substring or fuzzy matching."""

# These are semantic aliases, not a replacement for the user's source category.
CATEGORY_ALIASES = {
    '学习': '书籍学习', '电器数码': '数码', '话费网费': '通信',
    '网费': '通信', '衣服': '服饰', '住房': '居住',
}

# Complete notes only. Normalizing case/whitespace never changes stored evidence.
NOTE_GROUPS = {
    'meal:早餐': ('breakfast',),
    'meal:午餐': ('lunch',),
    'meal:晚餐': ('dinner', 'supper'),
    'meal:加餐': ('midnight meal', 'late night meal', 'late-night meal'),
    'item:咖啡': ('coffee', 'americano', 'espresso', 'cappuccino'),
    'item:茶饮': ('tea', 'milk tea', 'bubble tea', 'boba tea'),
    'item:饮料': ('cola', 'coke', 'coke cola', 'coca cola', 'coca-cola', 'coca coke', 'soda', 'juice', 'orange juice'),
    'item:酒饮': ('beer', 'beers', 'wine', 'whisky', 'whiskey'),
    'item:乳品': ('milk', 'yogurt', 'yoghurt'),
    'item:零食': ('ice cream', 'cookies', 'cookie', 'egg tart', 'chocolate', 'chips'),
    'item:餐食': ('bread', 'soup', 'pizza', 'noodles', 'instant noodles', 'dumplings', 'wonton', 'rice'),
    'item:日用品': ('tissue', 'tissues', 'laundry detergent', 'toothpaste', 'toothbrush', 'shampoo', 'soap'),
    'item:交通': ('bus fare', 'taxi fare', 'train ticket'),
    'item:书籍学习': ('stationery', 'textbook'),
    'item:数码': ('charging dock', 'phone charger', 'charging cable', 'usb cable'),
    'item:水电燃气': ('electricity', 'electricity bill', 'water bill', 'gas bill'),
}

# Ambiguous nouns need the existing source category as context.
CONTEXT_NOTES = {
    ('水电煤', 'water'): 'item:水电燃气',
    ('水电煤', 'hot water'): 'item:水电燃气',
    ('零食', 'water'): 'item:饮料',
    ('学习', 'pen'): 'item:书籍学习',
    ('学习', 'printing'): 'item:书籍学习',
    ('学习', 'notebook'): 'item:书籍学习',
    ('交通', 'subway'): 'item:交通',
    ('交通', 'metro'): 'item:交通',
    ('电器数码', 'watch stand'): 'item:数码',
    ('话费网费', 'network'): 'item:通信',
    ('话费网费', 'phone'): 'item:通信',
    ('话费', 'phone'): 'item:通信',
    ('电器数码', 'phone'): 'item:数码',
    ('娱乐', 'game'): 'item:娱乐',
}

NOTE_LOOKUP = {phrase: tag for tag, phrases in NOTE_GROUPS.items() for phrase in phrases}


def note_tags(row):
    """Return tags only when the *entire* expense note has an explicit mapping."""
    if row['kind'] != 'expense':
        return []
    evidence = (row['remark'] or '').strip()
    if len(evidence) > 240:
        return []
    key = ' '.join(evidence.casefold().split())
    tag = NOTE_LOOKUP.get(key) or CONTEXT_NOTES.get((row['category'], key))
    if tag is None:
        return []
    dimension, label = tag.split(':', 1)
    result = [{'dimension': dimension, 'label': label, 'field': 'remark', 'evidence': evidence}]
    if dimension == 'meal':
        result.append({'dimension': 'item', 'label': '餐食', 'field': 'remark', 'evidence': evidence})
    return result


def note_resolved(row):
    return bool(note_tags(row))
