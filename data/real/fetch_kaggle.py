"""Download Kaggle datasets used for realistic prices/names.  pip install kagglehub ; needs Kaggle auth (~/.kaggle/kaggle.json or env).

Slugs confirmed to exist via search (file/column schema NOT verified offline — the builder auto-detects and fails loudly):
  surajjha101/bigbasket-entire-product-list-28k-datapoints   (Indian grocery prices)
  ananysharma/indian-names-dataset                            (Indian names)
"""
import kagglehub

for slug in ("surajjha101/bigbasket-entire-product-list-28k-datapoints", "ananysharma/indian-names-dataset"):
    print(slug, "->", kagglehub.dataset_download(slug))
