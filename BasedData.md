#claude ignore this file

# Comprehensive Data Validation Test Data Generation Prompt

You are acting as a **data generation and error simulation engine** for a CSV-based data validation program.

---

## 1. Business Context

I am validating data between a **source CSV file** and a **destination CSV file** for a **single table: Employee Course Enrollment**.

Each record represents an employee enrolled in a course for a specific year.

---

## 2. Composite Key (Critical Requirement)

Each record must be uniquely identified using the following **composite key**:

- **Emp_ID**
- **Course Number**
- **Course Year**

### Composite Key Rules
- Records must be matched **only** using this composite key.
- All three key fields must match **after transformations**.
- Validation flow:
  1. Transform key fields as per rules.
  2. Match records using the transformed composite key.
  3. Only then compare non-key fields.
- Any of the following should result in a **record-level failure**:
  - Composite key mismatch
  - Missing composite key in destination
  - Duplicate composite key in source or destination

---

## 3. Input Artifacts (Assume These Exist)

You must logically follow these artifacts while generating data:

- `employee_source.csv`
- `employee_destination.csv`
- `column_mapping.csv`
- `value_mapping.csv`

❗ Do NOT modify the mapping or rules defined in these artifacts.

---

## 4. Task 1: Generate Source CSV

Create a **source CSV file** with **at least 15 records**.

### Mandatory Composite Key Columns (Source)
- Emp_ID (example: `E201`)
- Course_Number (example: `C-401`)
- Course_Year (example: `2024`)

### Additional Columns to Include
Include realistic employee and course attributes such as:
- Employee Name (string)
- Employment Status (`NEW`, `PENDING`, `COMPLETED`)
- Result (`TRUE`, `FALSE`)
- Course Type (`CLASSROOM`, `ONLINE`, `WEEKEND`)
- Course Fee (decimal)
- Join Date (`YYYY-MM-DD`)
- Active Flag (`YES`, `NO`)

Ensure:
- Correct data types
- Business-friendly values
- No duplicate composite keys

---

## 5. Task 2: Generate Destination CSV (Baseline)

Generate a **destination CSV** by transforming the source data using:
- Column name mappings from `column_mapping.csv`
- Value mappings from `value_mapping.csv`
- Transformation rules such as:
  - Removing prefixes (`E101 → 101`, `C-401 → 401`)
  - Mapping text values to codes
  - Date format changes
  - Boolean normalization

✅ Ensure **most records are correctly transformed and should PASS validation**.

---

## 6. Task 3: Introduce Intentional Errors in Destination

Deliberately introduce **controlled and realistic errors** in the destination file to test the validation program.

Introduce **at least one error from each category below**.

---

### A. Composite Key Errors (Highest Priority)
Examples:
- Course Year mismatch for same Emp_ID + Course Number
- Course Number mismatch after prefix removal
