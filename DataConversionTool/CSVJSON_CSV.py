import json
import csv
import os

input_file = "input.csv"
output_dir = "output"

# Create output directory if it doesn't exist
os.makedirs(output_dir, exist_ok=True)

output_file = os.path.join(output_dir, os.path.basename(input_file))

rows = []

# Read line‑delimited JSON
with open(input_file, "r") as f:
    for line in f:
        if line.strip():
            rows.append(json.loads(line))

# Write CSV to output folder with same filename
with open(output_file, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

print(f"Conversion complete. File written to {output_file}")
