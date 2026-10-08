# OfferWatch offline prototype
Python 3.10+; standard library only. No installation or API key required.

From this folder:
```
python -m unittest -v
python compare.py before.json after.json comparison_demo.html
```
Open comparison_demo.html locally. All fixtures are synthetic. The report makes no real-world change claims. Validation requires timezone-aware observations, reviewed successful captures and HTTPS reference URLs; it compares supplied records rather than checking their truth. No network calls are made. A failed check never implies removal. Stable event IDs are generated, but persistent deduplication and delivery state are not implemented. Unknown fields are not full schema enforcement; production hardening and independently reviewed collection are outstanding.

The business baseline report separately uses primary-source retrieved observations. Its observation date records research retrieval, not a guaranteed origin refresh time. Revalidate before customer use. No full third-party page copies are bundled.
