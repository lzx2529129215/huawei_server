import pytest
from memscenes.scene4_roles import memory_roles
from memscenes.runner import case_definitions, cases_hash

BUNDLES = [f'io.memscene.scene4.workload.a{i}' for i in range(1, 9)]


def config():
    return dict(execution_profile='legacy_peak_transition', oracle_profile='peak_transition_os_snapshot',
                fixed_background_donors=BUNDLES[:2], fixed_background_donor_mib=6144, daily_mib=2048)


def test_totals_preserved_and_only_six_new_allocation_targets():
    r = memory_roles(config(), BUNDLES, 4864, 1200)
    assert r['daily_total_mib'] == 24576
    assert r['peak_total_mib'] == 41472
    assert r['incremental_bytes'] == 16896 * 1024**2
    assert r['peak_targets'] == BUNDLES[2:]
    assert r['reclaim_candidates'] == BUNDLES[:2]
    assert not set(r['peak_targets']) & set(r['reclaim_candidates'])
    assert all(r['per_app'][b]['daily_mib'] == r['per_app'][b]['peak_mib'] == 6144 for b in BUNDLES[:2])


def test_role_membership_does_not_rotate_with_start_order():
    r = memory_roles(config(), BUNDLES[1:] + BUNDLES[:1], 4864, 1200)
    assert r['reclaim_candidates'] == BUNDLES[:2]
    assert r['peak_targets'][0] == BUNDLES[2]


@pytest.mark.parametrize('change', [dict(fixed_background_donors=BUNDLES[:1]),
    dict(fixed_background_donors=[BUNDLES[0], BUNDLES[0]]),
    dict(fixed_background_donors=['com.huawei.hmos.filemanager', BUNDLES[0]]),
    dict(execution_profile='default'), dict(fixed_background_donor_mib=1000)])
def test_invalid_roles_refused(change):
    c = config(); c.update(change)
    with pytest.raises(ValueError): memory_roles(c, BUNDLES, 4864, 1200)


def test_existing_uniform_case_unchanged():
    r = memory_roles({'daily_mib': 3072}, BUNDLES, 5184, 1200)
    assert r['peak_targets'] == r['reclaim_candidates'] == BUNDLES
    assert r['daily_total_mib'] == 24576 and r['peak_total_mib'] == 41472
    assert not r['enabled']


def test_frozen_case_hash_includes_role_memory():
    c = config(); c.update(peak_mib=4864, file_mib=1200, hold_seconds=60,
                          case_repetitions={'popup':1}, seed=20261005)
    cfg = dict(scene4=c, test_apps=BUNDLES, dram_gib=32)
    first = case_definitions(cfg, 'popup', 'scene4')
    assert first[0]['memory_roles']['donors'] == BUNDLES[:2]
    c['fixed_background_donor_mib'] = 6080
    assert cases_hash(first) != cases_hash(case_definitions(cfg, 'popup', 'scene4'))
