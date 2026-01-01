"""Frozen, evidence-based classification for qianji-taxonomy/1.

Nodes describe purchases, not their necessity, motivation or actual use. A bill
has at most one value at each level; incompatible labels retain their common
ancestor. Never edit this recipe to change already-published classifications.
"""
from copy import deepcopy
import hashlib
import json

TAXONOMY_VERSION = 'qianji-taxonomy/1'

# Stable machine identifiers; labels can be displayed without parsing an id.
_TREE = {
    'food': ('食品饮品与烟酒', {
        'meals': ('餐食', {'breakfast': '早餐', 'lunch': '午餐', 'dinner': '晚餐', 'extra': '加餐'}),
        'drinks': ('饮品', {'coffee': '咖啡', 'tea': '茶饮', 'soft': '其他饮料', 'alcohol': '酒饮', 'dairy': '乳品'}),
        'snacks': ('零食', {}), 'fruit': ('水果', {}), 'tobacco_alcohol': ('烟酒', {}),
    }),
    'home': ('居住生活', {
        'supplies': ('日用品', {'paper': '纸品', 'laundry': '洗衣用品', 'hygiene': '洗护用品'}),
        'utilities': ('水电燃气', {'electricity': '电费', 'water': '水费', 'gas': '燃气费'}),
        'housing': ('居住', {'rent': '房租'}),
    }),
    'transport': ('交通出行', {'transport': ('交通', {'bus': '公交', 'subway': '地铁', 'taxi': '出租车', 'railway': '铁路'}), 'travel': ('旅行', {})}),
    'health': ('健康与运动', {'medical': ('医疗', {}), 'sport': ('运动', {})}),
    'learning': ('教育学习', {'learning': ('学习用品与服务', {'books': '书籍', 'stationery': '文具', 'printing': '打印'})}),
    'digital': ('数码通信', {'devices': ('数码', {'accessories': '数码配件'}), 'communication': ('通信', {})}),
    'personal': ('衣着与护理', {'clothing': ('服饰', {}), 'haircut': ('理发', {})}),
    'leisure': ('休闲娱乐', {'entertainment': ('娱乐', {})}),
    'membership': ('会员服务', {'subscription': ('会员订阅', {})}),
}


def _nodes():
    result = []
    for first, (label, children) in _TREE.items():
        result.append({'id': first, 'parent_id': None, 'level': 1, 'label': label})
        for second, (label, leaves) in children.items():
            parent = first + '.' + second
            result.append({'id': parent, 'parent_id': first, 'level': 2, 'label': label})
            result.extend({'id': parent + '.' + key, 'parent_id': parent, 'level': 3, 'label': value}
                          for key, value in leaves.items())
    return result


_NODES = _nodes()
_NODE_MAP = {node['id']: node for node in _NODES}
_ITEM_NODES = {
    '餐食': 'food.meals', '咖啡': 'food.drinks.coffee', '茶饮': 'food.drinks.tea',
    '饮料': 'food.drinks.soft', '酒饮': 'food.drinks.alcohol', '乳品': 'food.drinks.dairy',
    '零食': 'food.snacks', '水果': 'food.fruit', '日用品': 'home.supplies',
    '水电燃气': 'home.utilities', '居住': 'home.housing', '交通': 'transport.transport',
    '医疗': 'health.medical', '运动': 'health.sport', '书籍学习': 'learning.learning',
    '数码': 'digital.devices', '通信': 'digital.communication', '服饰': 'personal.clothing',
    '娱乐': 'leisure.entertainment', '会员订阅': 'membership.subscription',
}
_MEAL_NODES = {label: 'food.meals.' + key for key, label in _TREE['food'][1]['meals'][1].items()}

# Freeze the effective rule vocabulary rather than importing mutable analysis
# code. The original category is retained by the fact and never overwritten.
_CATEGORY_ITEMS = {
    '三餐': '餐食', '早餐': '餐食', '午餐': '餐食', '晚餐': '餐食', '餐饮': '餐食',
    '咖啡': '咖啡', '奶茶': '茶饮', '茶饮': '茶饮', '零食': '零食', '水果': '水果',
    '牛奶': '乳品', '日用品': '日用品', '交通': '交通', '医疗': '医疗',
    '付费会员': '会员订阅', '水电煤': '水电燃气', '运动': '运动', '书籍': '书籍学习',
    '数码': '数码', '服饰': '服饰', '房租': '居住', '话费': '通信', '娱乐': '娱乐',
    '学习': '书籍学习', '电器数码': '数码', '话费网费': '通信', '网费': '通信',
    '衣服': '服饰', '住房': '居住',
}
_CATEGORY_NODES = {key: _ITEM_NODES[value] for key, value in _CATEGORY_ITEMS.items()}
_CATEGORY_NODES.update({
    '早餐': _MEAL_NODES['早餐'], '午餐': _MEAL_NODES['午餐'], '晚餐': _MEAL_NODES['晚餐'],
    '房租': 'home.housing.rent', '书籍': 'learning.learning.books',
    '理发': 'personal.haircut', '旅行': 'transport.travel',
    '烟酒': 'food.tobacco_alcohol',
})
_NOTE_GROUPS = {
    'meal:早餐': ('breakfast',), 'meal:午餐': ('lunch',),
    'meal:晚餐': ('dinner', 'supper'), 'meal:加餐': ('midnight meal', 'late night meal', 'late-night meal'),
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
_NOTE_LOOKUP = {phrase: tag for tag, phrases in _NOTE_GROUPS.items() for phrase in phrases}
_CHINESE_NOTES = {
    '早餐': 'meal:早餐', '早饭': 'meal:早餐', '午餐': 'meal:午餐', '午饭': 'meal:午餐',
    '晚餐': 'meal:晚餐', '晚饭': 'meal:晚餐', '夜宵': 'meal:加餐', '宵夜': 'meal:加餐', '加餐': 'meal:加餐',
    '咖啡': 'item:咖啡', '美式咖啡': 'item:咖啡', '拿铁': 'item:咖啡',
    '奶茶': 'item:茶饮', '茶': 'item:茶饮', '茶饮': 'item:茶饮',
    '可乐': 'item:饮料', '汽水': 'item:饮料', '果汁': 'item:饮料', '橙汁': 'item:饮料',
    '啤酒': 'item:酒饮', '葡萄酒': 'item:酒饮', '牛奶': 'item:乳品', '酸奶': 'item:乳品',
    '冰淇淋': 'item:零食', '饼干': 'item:零食', '巧克力': 'item:零食', '薯片': 'item:零食',
    '面包': 'item:餐食', '米饭': 'item:餐食', '面条': 'item:餐食', '饺子': 'item:餐食',
    '纸巾': 'item:日用品', '洗衣液': 'item:日用品', '牙膏': 'item:日用品', '牙刷': 'item:日用品',
    '洗发水': 'item:日用品', '公交': 'item:交通', '公交车费': 'item:交通',
    '地铁': 'item:交通', '地铁费': 'item:交通', '出租车': 'item:交通', '打车': 'item:交通',
    '火车票': 'item:交通', '高铁票': 'item:交通', '书籍': 'item:书籍学习', '教材': 'item:书籍学习',
    '文具': 'item:书籍学习', '打印': 'item:书籍学习', '电费': 'item:水电燃气',
    '水费': 'item:水电燃气', '燃气费': 'item:水电燃气', '充电器': 'item:数码', '充电线': 'item:数码',
}
_CONTEXT_NOTES = {
    ('水电煤', 'water'): 'item:水电燃气', ('水电煤', 'hot water'): 'item:水电燃气',
    ('零食', 'water'): 'item:饮料', ('学习', 'pen'): 'item:书籍学习',
    ('学习', 'printing'): 'item:书籍学习', ('学习', 'notebook'): 'item:书籍学习',
    ('交通', 'subway'): 'item:交通', ('交通', 'metro'): 'item:交通',
    ('电器数码', 'watch stand'): 'item:数码', ('话费网费', 'network'): 'item:通信',
    ('话费网费', 'phone'): 'item:通信', ('话费', 'phone'): 'item:通信',
    ('电器数码', 'phone'): 'item:数码', ('娱乐', 'game'): 'item:娱乐',
}
_DETAIL_GROUPS = {
    'home.supplies.paper': ('tissue', 'tissues', '纸巾'),
    'home.supplies.laundry': ('laundry detergent', '洗衣液'),
    'home.supplies.hygiene': ('toothpaste', 'toothbrush', 'shampoo', 'soap', '牙膏', '牙刷', '洗发水'),
    'home.utilities.electricity': ('electricity', 'electricity bill', '电费'),
    'home.utilities.water': ('water bill', '水费'),
    'home.utilities.gas': ('gas bill', '燃气费'),
    'transport.transport.bus': ('bus fare', '公交', '公交车费'),
    'transport.transport.subway': ('subway', 'metro', '地铁', '地铁费'),
    'transport.transport.taxi': ('taxi fare', '出租车', '打车'),
    'transport.transport.railway': ('train ticket', '火车票', '高铁票'),
    'learning.learning.books': ('textbook', '书籍', '教材'),
    'learning.learning.stationery': ('stationery', 'pen', 'notebook', '文具'),
    'learning.learning.printing': ('printing', '打印'),
    'digital.devices.accessories': ('charging dock', 'phone charger', 'charging cable', 'usb cable', 'watch stand', '充电器', '充电线'),
}
_DETAIL_LOOKUP = {phrase: node for node, phrases in _DETAIL_GROUPS.items() for phrase in phrases}
_EXTRA = ('channel', 'context', 'meal', 'merchant')
_SEMANTICS = {
    'grain': 'one transaction; each dimension has at most one effective value',
    'amount_allocation': 'no allocation or duplication across candidate labels',
    'scope': 'expense and refund; income and transfer are not applicable',
    'missing': 'null means unavailable; a missing third level is not an invented leaf',
    'precedence': 'confirmed labels take precedence over automatic labels',
    'proposed': 'visible only as candidates and evidence, never an effective value',
    'remark_refinement': 'an exact complete remark can replace a coarse source category inside the same top-level domain',
    'meal_context': 'automatic or confirmed meal occasions are orthogonal to an effective drink subtype; a confirmed item can still replace the source subtype',
    'conflict': 'different top-level domains or incompatible explicit leaves retain common ancestors; deeper levels are ambiguous',
    'refund': 'classify the refund own evidence; do not inherit a related purchase',
    'source_category': 'preserved separately; this hierarchy does not change the source category',
    'review': 'processed published labels replace old rules; reviewed records receive no new remark rules; rejected old labels are not regenerated',
    'new_category_nodes': 'literal source categories absent from the legacy item vocabulary can supply their own nodes only on unreviewed records',
    'time': 'meal labels require explicit evidence; clock time never implies meal or purpose',
}
RECIPE = hashlib.sha256(json.dumps({
    'version': TAXONOMY_VERSION, 'nodes': _NODES, 'items': _ITEM_NODES,
    'category_items': _CATEGORY_ITEMS, 'category_nodes': _CATEGORY_NODES,
    'notes': _NOTE_GROUPS, 'chinese_notes': _CHINESE_NOTES, 'remark_details': _DETAIL_GROUPS,
    'context_notes': sorted((list(k), v) for k, v in _CONTEXT_NOTES.items()),
    'semantics': _SEMANTICS, 'algorithm': 'exact-remark-refinement-confirmed-first-1',
}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def taxonomy_catalog():
    return deepcopy({'version': TAXONOMY_VERSION, 'recipe': RECIPE, 'nodes': _NODES,
                     'dimensions': ['category_l1', 'category_l2', 'category_l3', *_EXTRA],
                     'semantics': _SEMANTICS})


def _tag(dimension, label, field, evidence):
    return {'dimension': dimension, 'label': label, 'field': field, 'evidence': evidence,
            'method': 'rule', 'state': 'automatic'}


def _note_key(row):
    evidence = (row.get('remark') or '').strip()
    return evidence, ' '.join(evidence.casefold().split()) if len(evidence) <= 240 else None


def _frozen_rules(row):
    tags = []
    category = row.get('category')
    if category in _CATEGORY_ITEMS:
        tags.append(_tag('item', _CATEGORY_ITEMS[category], 'category', category))
    if row.get('kind') != 'expense':
        return tags
    evidence, key = _note_key(row)
    match = _NOTE_LOOKUP.get(key) or _CONTEXT_NOTES.get((category, key)) or _CHINESE_NOTES.get(key)
    if match:
        dimension, label = match.split(':', 1)
        additions = [_tag(dimension, label, 'remark', evidence)]
        if dimension == 'meal':
            additions.append(_tag('item', '餐食', 'remark', evidence))
        keys = {(t['dimension'], t['label']) for t in tags}
        tags.extend(t for t in additions if (t['dimension'], t['label']) not in keys)
    return tags


def _new_remark_rules(row, tags, authoritative):
    if row.get('kind') != 'expense':
        return []
    evidence, key = _note_key(row)
    additions = []
    if authoritative and key in _CHINESE_NOTES:
        dimension, label = _CHINESE_NOTES[key].split(':', 1)
        additions.append(_tag(dimension, label, 'remark', evidence))
        if dimension == 'meal':
            additions.append(_tag('item', '餐食', 'remark', evidence))
    node = _DETAIL_LOOKUP.get(key)
    if key in ('water', 'hot water') and row.get('category') == '水电煤':
        node = 'home.utilities.water'
    if node:
        parent = _NODE_MAP[node]['parent_id']
        # Require an effective supporting category/item; ambiguous English
        # words such as notebook/subway are not classified without context.
        supported = any(tag.get('state') in ('automatic', 'confirmed') and tag.get('dimension') == 'item'
                        and _ITEM_NODES.get(tag.get('label')) == parent for tag in [*tags, *additions])
        if supported:
            additions.append({**_tag('remark_detail', _NODE_MAP[node]['label'], 'remark', evidence), '_node': node})
    return additions


def _evidence(tags):
    # Preserve quoted source text and trust state, without mutable model internals.
    values = [{key: tag.get(key) for key in ('dimension', 'label', 'field', 'evidence', 'method', 'state')}
              for tag in tags]
    unique = {json.dumps(value, ensure_ascii=False, sort_keys=True): value for value in values}
    return [unique[key] for key in sorted(unique)]


def _path(node):
    result = []
    while node is not None:
        result.append(node)
        node = _NODE_MAP[node]['parent_id']
    return list(reversed(result))


def _decision(value=None, state='unknown', candidates=(), evidence=()):
    return {'value': value, 'state': state, 'candidates': sorted(set(candidates)),
            'evidence': _evidence(evidence)}


def _preferred(tags):
    confirmed = [tag for tag in tags if tag.get('state') == 'confirmed']
    return confirmed or [tag for tag in tags if tag.get('state') == 'automatic']


def _hierarchy(tags):
    # "Coffee / breakfast" identifies a drink and its meal occasion. Legacy
    # rules additionally emitted item:餐食 for every meal note; that inherited
    # generic tag must not compete with an explicit source drink subtype.
    confirmed_items = [tag for tag in tags if tag.get('dimension') == 'item' and tag.get('state') == 'confirmed']
    drink_category = (all(tag['_node'].startswith('food.drinks.') for tag in confirmed_items) if confirmed_items else
                      any(tag.get('field') == 'category' and tag.get('state') in ('automatic', 'confirmed')
                          and tag['_node'].startswith('food.drinks.') for tag in tags))
    meal_quotes = {tag.get('evidence') for tag in tags if tag.get('dimension') == 'meal'
                   and tag.get('state') in ('automatic', 'confirmed')}
    if drink_category:
        tags = [tag for tag in tags if not (
            (tag.get('dimension') == 'meal' and tag.get('state') in ('automatic', 'confirmed'))
            or (tag.get('state') == 'automatic' and tag.get('field') == 'remark'
                and tag.get('evidence') in meal_quotes and tag['_node'] == 'food.meals'))]
    selected = _preferred(tags)
    proposals = [tag for tag in tags if tag.get('state') == 'proposed']
    # Within one domain an explicit complete remark carries more purchase
    # detail than a coarse source category. Keep both pieces of evidence.
    note_paths = [_path(tag['_node']) for tag in selected if tag.get('field') == 'remark']
    used = [tag for tag in selected if not (
        tag.get('field') == 'category' and _NODE_MAP[tag['_node']]['level'] < 3
        and any(path[0] == _path(tag['_node'])[0] for path in note_paths))]
    paths = [_path(tag['_node']) for tag in used]
    proposed_paths = [_path(tag['_node']) for tag in proposals]
    result = {}
    ambiguous = False
    for depth in range(3):
        values = {path[depth] for path in paths if len(path) > depth}
        proposed = {path[depth] for path in proposed_paths if len(path) > depth}
        if len(values) > 1:
            ambiguous = True
        if ambiguous:
            decision = _decision(state='ambiguous', candidates=values, evidence=selected)
        elif values:
            value = next(iter(values))
            decision = _decision(value, 'known', [value], selected)
        else:
            decision = _decision(candidates=proposed, evidence=selected + proposals)
        result['category_l' + str(depth + 1)] = decision
    return result


def classify(row, entry=None):
    """Build deterministic decisions without mutating the source or labels.

    A processed entry is authoritative even if its labels are empty, because a
    human may have rejected a rule. Unsupported raw categories can still use
    newly defined literal nodes; they had no legacy category rule to reject.
    """
    dimensions = ['category_l1', 'category_l2', 'category_l3', *_EXTRA]
    if row.get('kind') not in ('expense', 'refund'):
        return {name: _decision(state='not_applicable') for name in dimensions}
    authoritative = entry is not None and entry.get('rules_processed') is True
    tags = list(entry.get('labels', [])) if authoritative else _frozen_rules(row)
    if entry is not None and not authoritative:
        tags.extend(tag for tag in entry.get('labels', []) if tag.get('state') in ('confirmed', 'proposed'))
    if entry is None or entry.get('model_status') != 'reviewed':
        tags.extend(_new_remark_rules(row, tags, authoritative))
    category = row.get('category')
    hierarchy = []
    for tag in tags:
        dimension, label = tag.get('dimension'), tag.get('label')
        node = (tag.get('_node') if dimension == 'remark_detail' else _ITEM_NODES.get(label)
                if dimension == 'item' else _MEAL_NODES.get(label) if dimension == 'meal' else None)
        if node:
            # A category's explicit detail (e.g. books or rent) is safe only
            # while its corresponding category rule remains effective.
            if tag.get('field') == 'category' and tag.get('evidence') == category and label == _CATEGORY_ITEMS.get(category):
                node = _CATEGORY_NODES.get(category, node)
            hierarchy.append({**tag, '_node': node})
    if (category in _CATEGORY_NODES and category not in _CATEGORY_ITEMS
            and (entry is None or entry.get('model_status') != 'reviewed')):
        hierarchy.append({**_tag('source_category', category, 'category', category), '_node': _CATEGORY_NODES[category]})
    result = _hierarchy(hierarchy)
    for dimension in _EXTRA:
        current = [tag for tag in tags if tag.get('dimension') == dimension]
        selected = _preferred(current)
        values = {tag['label'] for tag in selected}
        if len(values) == 1:
            value = next(iter(values))
            result[dimension] = _decision(value, 'known', values, selected)
        elif len(values) > 1:
            result[dimension] = _decision(state='ambiguous', candidates=values, evidence=selected)
        else:
            proposed = [tag for tag in current if tag.get('state') == 'proposed']
            result[dimension] = _decision(candidates=[tag['label'] for tag in proposed], evidence=proposed)
    return result
