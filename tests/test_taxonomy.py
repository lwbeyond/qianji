"""Synthetic, non-monetary examples for the frozen classification contract."""
import json
import unittest

from qianji.taxonomy_v1 import RECIPE, TAXONOMY_VERSION, classify, taxonomy_catalog


def bill(category='三餐', remark='lunch', kind='expense'):
    return {'category': category, 'remark': remark, 'kind': kind}


def tag(dimension, label, state='automatic', field='remark', evidence='synthetic'):
    return {'dimension': dimension, 'label': label, 'state': state, 'field': field,
            'evidence': evidence, 'method': {'automatic': 'rule', 'confirmed': 'manual', 'proposed': 'model'}[state]}


class TaxonomyTests(unittest.TestCase):
    def test_explicit_meal_refines_three_levels_without_mutating_source(self):
        row = bill(remark=' LUNCH ')
        before = dict(row)
        value = classify(row)
        self.assertEqual([value['category_l'+str(n)]['value'] for n in (1, 2, 3)],
                         ['food', 'food.meals', 'food.meals.lunch'])
        self.assertEqual(value['meal']['value'], '午餐')
        self.assertEqual(value['meal']['evidence'][0]['evidence'], 'LUNCH')
        self.assertEqual(row, before)

    def test_unknown_third_level_is_not_fabricated(self):
        value = classify(bill('交通', ''))
        self.assertEqual(value['category_l2']['value'], 'transport.transport')
        self.assertEqual(value['category_l3']['state'], 'unknown')
        self.assertIsNone(value['category_l3']['value'])
        self.assertEqual(classify(bill('其它', ''))['category_l1']['state'], 'unknown')

    def test_explicit_note_refines_coarse_source_category_in_same_domain(self):
        value = classify(bill('零食', 'milk tea'))
        self.assertEqual(value['category_l1']['value'], 'food')
        self.assertEqual(value['category_l2']['value'], 'food.drinks')
        self.assertEqual(value['category_l3']['value'], 'food.drinks.tea')
        self.assertEqual({e['field'] for e in value['category_l3']['evidence']}, {'category', 'remark'})

    def test_incompatible_explicit_leaves_remain_ambiguous(self):
        value = classify(bill('咖啡', 'milk tea'))
        self.assertEqual(value['category_l2']['value'], 'food.drinks')
        self.assertEqual(value['category_l3']['state'], 'ambiguous')
        self.assertIsNone(value['category_l3']['value'])

    def test_meal_occasion_does_not_change_explicit_drink_subtype(self):
        value = classify(bill('咖啡', 'breakfast'))
        self.assertEqual(value['category_l3']['value'], 'food.drinks.coffee')
        self.assertEqual(value['meal']['value'], '早餐')

    def test_confirmed_meal_is_orthogonal_to_explicit_drink_with_fallback_rules(self):
        entry = {'rules_processed': False, 'labels': [tag('meal', '早餐', 'confirmed', evidence='breakfast')]}
        value = classify(bill('咖啡', 'breakfast'), entry)
        self.assertEqual(value['category_l3']['value'], 'food.drinks.coffee')
        self.assertEqual(value['meal']['value'], '早餐')
        self.assertEqual(value['meal']['evidence'][0]['state'], 'confirmed')

    def test_confirmed_item_can_replace_drink_and_use_confirmed_meal(self):
        entry = {'rules_processed': True, 'model_status': 'reviewed', 'labels': [
            tag('item', '咖啡', field='category', evidence='咖啡'),
            tag('item', '餐食', 'confirmed', evidence='breakfast'),
            tag('meal', '早餐', 'confirmed', evidence='breakfast'),
        ]}
        value = classify(bill('咖啡', 'breakfast'), entry)
        self.assertEqual(value['category_l3']['value'], 'food.meals.breakfast')
        self.assertTrue(all(e['state'] == 'confirmed' for e in value['category_l3']['evidence']))

    def test_explicit_alcohol_note_refines_combined_tobacco_alcohol_category(self):
        value = classify(bill('烟酒', 'beer'))
        self.assertEqual(value['category_l3']['value'], 'food.drinks.alcohol')

    def test_cross_domain_conflict_preserves_no_false_ancestor(self):
        value = classify(bill('日用品', 'bread'))
        self.assertEqual(value['category_l1']['state'], 'ambiguous')
        self.assertTrue(all(value['category_l'+str(n)]['value'] is None for n in (1, 2, 3)))

    def test_confirmed_hierarchy_overrides_automatic_conflict(self):
        entry = {'rules_processed': True, 'labels': [tag('item', '零食'), tag('item', '茶饮', 'confirmed')]}
        value = classify(bill('零食', 'milk tea'), entry)
        self.assertEqual(value['category_l3']['value'], 'food.drinks.tea')
        self.assertTrue(all(e['state'] == 'confirmed' for e in value['category_l3']['evidence']))

    def test_proposed_labels_are_candidates_never_effective_values(self):
        entry = {'rules_processed': True, 'labels': [tag('item', '茶饮', 'proposed'), tag('merchant', '合成店', 'proposed')]}
        value = classify(bill('其它', ''), entry)
        self.assertEqual(value['category_l1']['state'], 'unknown')
        self.assertEqual(value['category_l1']['candidates'], ['food'])
        self.assertIsNone(value['merchant']['value'])
        self.assertEqual(value['merchant']['candidates'], ['合成店'])
        self.assertEqual(value['merchant']['evidence'][0]['state'], 'proposed')

    def test_published_rejection_is_not_regenerated_from_category_or_remark(self):
        value = classify(bill(), {'rules_processed': True, 'labels': []})
        self.assertTrue(all(decision['value'] is None for decision in value.values()))
        self.assertTrue(all(decision['state'] == 'unknown' for decision in value.values()))

    def test_each_extra_dimension_resolves_confirmed_and_multiple_values(self):
        entry = {'rules_processed': True, 'labels': [
            tag('channel', '网购'), tag('channel', '线下', 'confirmed'),
            tag('context', '工作', 'confirmed'), tag('context', '学习', 'confirmed'),
            tag('merchant', '合成甲', 'confirmed'), tag('merchant', '合成乙', 'confirmed'),
        ]}
        value = classify(bill(), entry)
        self.assertEqual(value['channel']['value'], '线下')
        self.assertEqual(value['context']['state'], 'ambiguous')
        self.assertEqual(value['merchant']['state'], 'ambiguous')

    def test_refund_uses_own_category_and_never_infers_meal_from_time(self):
        value = classify(bill('咖啡', 'breakfast', 'refund'))
        self.assertEqual(value['category_l3']['value'], 'food.drinks.coffee')
        self.assertIsNone(value['meal']['value'])
        for kind in ('income', 'transfer'):
            self.assertTrue(all(d['state'] == 'not_applicable' for d in classify(bill(kind=kind)).values()))

    def test_unmatched_and_negated_notes_do_not_activate_partial_rules(self):
        for remark in ('not beer', 'coffee and tea', 'after work', 'water', 'balance'):
            value = classify(bill('其它', remark))
            self.assertIsNone(value['category_l1']['value'], remark)
        self.assertEqual(classify(bill('水电煤', 'water'))['category_l2']['value'], 'home.utilities')

    def test_literal_source_details_and_new_categories(self):
        for category, node in [('房租', 'home.housing.rent'), ('书籍', 'learning.learning.books'), ('早餐', 'food.meals.breakfast')]:
            self.assertEqual(classify(bill(category, ''))['category_l3']['value'], node)
        self.assertEqual(classify(bill('理发', ''), {'rules_processed': True, 'labels': []})['category_l2']['value'], 'personal.haircut')

    def test_chinese_complete_notes_and_source_supported_details(self):
        examples = [('三餐', '午饭', 'food.meals.lunch'), ('零食', '奶茶', 'food.drinks.tea'),
                    ('交通', 'subway', 'transport.transport.subway'), ('水电煤', 'electricity', 'home.utilities.electricity'),
                    ('学习', 'notebook', 'learning.learning.stationery'), ('日用品', '洗衣液', 'home.supplies.laundry')]
        for category, remark, node in examples:
            self.assertEqual(classify(bill(category, remark))['category_l3']['value'], node)
        self.assertIsNone(classify(bill('其它', 'notebook'))['category_l3']['value'])
        self.assertIsNone(classify(bill('其它', '不是奶茶'))['category_l3']['value'])

    def test_new_chinese_rules_do_not_override_reviewed_entry(self):
        entry = {'rules_processed': True, 'labels': [], 'model_status': 'reviewed'}
        value = classify(bill('三餐', '午饭'), entry)
        self.assertTrue(all(d['value'] is None for d in value.values()))
        self.assertIsNone(classify(bill('理发', ''), entry)['category_l2']['value'])
        entry['model_status'] = 'pending'
        self.assertEqual(classify(bill('三餐', '午饭'), entry)['category_l3']['value'], 'food.meals.lunch')

    def test_legacy_published_category_supports_new_precise_note_detail(self):
        entry = {'rules_processed': True, 'labels': [tag('item', '水电燃气', field='category', evidence='水电煤')]}
        value = classify(bill('水电煤', 'water bill'), entry)
        self.assertEqual(value['category_l3']['value'], 'home.utilities.water')

    def test_catalog_is_serializable_stable_and_detached(self):
        catalog = taxonomy_catalog()
        self.assertEqual(catalog['version'], TAXONOMY_VERSION)
        self.assertEqual(len(RECIPE), 64)
        nodes = {node['id']: node for node in catalog['nodes']}
        self.assertEqual(len(nodes), len(catalog['nodes']))
        for node in nodes.values():
            if node['parent_id']:
                self.assertEqual(nodes[node['parent_id']]['level'], node['level'] - 1)
        json.dumps(catalog, ensure_ascii=False)
        catalog['nodes'][0]['label'] = 'mutated'
        self.assertNotEqual(taxonomy_catalog()['nodes'][0]['label'], 'mutated')


if __name__ == '__main__':
    unittest.main()
