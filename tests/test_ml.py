import numpy as np
import pytest

from munim.ledger.chain import Ledger
from munim.ml.credit_model import (FEATURES, CreditModel, auc, brier, build_samples, features_at, score_customer,
                                   temporal_backtest, weekly_cutoffs)
from munim.ml.federated import fedavg, local_update, run_round

DAY = 86400.0


def test_auc_known_values():
    assert auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert auc([0, 0, 1, 1], [0.9, 0.8, 0.2, 0.1]) == 0.0
    assert auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]) == 0.5          # ties -> 0.5
    assert auc([1, 1], [0.2, 0.3]) is None


def test_features_never_see_the_future():
    adds = [(0.0, 100000), (10 * DAY, 50000)]
    pays = [(5 * DAY, 20000), (30 * DAY, 99999999)]               # a huge payment AFTER the cutoff
    f = features_at(adds, pays, 12 * DAY)
    g = features_at(adds, pays[:1], 12 * DAY)
    assert f == g and len(f) == len(FEATURES)
    assert features_at(adds, [(5 * DAY, 150000)], 12 * DAY) is None     # nothing owed -> no sample


def test_lr_learns_separable_signal_and_roundtrips():
    rng = np.random.default_rng(0)
    X = rng.random((400, len(FEATURES)))
    y = (X[:, 1] < 0.4).astype(float)                              # pays soon if paid recently
    m = CreditModel().fit(X, y)
    assert auc(y, m.predict(X)) > 0.95 and m.w[1] < 0
    m2 = CreditModel.from_json(m.to_json())
    assert np.allclose(m2.predict(X), m.predict(X))
    assert m.explain(X[0])[0][0] == "days_since_pay"
    with pytest.raises(ValueError):
        CreditModel.from_json('{"w":[1],"b":0,"features":["x"]}')


def test_fedavg_is_sample_weighted():
    a = CreditModel(np.ones(len(FEATURES)), 1.0, 100)
    b = CreditModel(np.zeros(len(FEATURES)), 0.0, 300)
    g = fedavg([a, b, None])
    assert np.allclose(g.w, 0.25) and g.b == pytest.approx(0.25) and g.n_train == 400
    assert fedavg([None]) is None


def seeded(tmp_path, i=0):
    import sys
    sys.path.insert(0, ".")
    from data.seed import seed
    return seed(str(tmp_path / f"s{i}.db"), f"s{i}", 90, 2610 + i)


def test_backtest_and_federation_run_on_seeded_ledgers(tmp_path):
    L = [seeded(tmp_path, i) for i in range(3)]
    r = temporal_backtest(L[0])
    assert r["n_train"] >= 20 and 0.0 <= r["auc"] <= 1.0 and r["brier_model"] < 0.5
    g = run_round(L, rounds=2)
    assert g is not None and g.n_train > 100
    c = L[0].customers()[0]
    rich = [x for x in L[0].customers() if L[0].balance(x.id) > 0][0]
    now = max(e.ts for e in L[0].entries())
    s = score_customer(L[0], rich.id, now, g)
    assert s is not None and 0.0 <= s[0] <= 1.0 and s[1]


def test_local_update_respects_min_samples_and_noise_clip(tmp_path):
    tiny = Ledger(str(tmp_path / "t.db"), "s")
    c = tiny.add_customer("A B")
    tiny.add_udhaar(c.id, 1000)
    assert local_update(tiny, None) is None                        # too little data: contributes nothing
    L = seeded(tmp_path)
    g = CreditModel()
    m = local_update(L, g, clip=0.01)
    assert float(np.linalg.norm(m.w - g.w)) <= 0.0101               # update clipped
