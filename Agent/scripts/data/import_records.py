import csv
from db.mysql_client import MySQLClient

def import_csv_to_mysql(csv_path="../data/external/records.csv"):
    conn = MySQLClient.get_connection()
    cursor = conn.cursor()
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # 跳过表头
        for row in reader:
            if len(row) < 6:
                continue
            user_id = row[0].strip('"')
            feature = row[1].strip('"')
            efficiency = row[2].strip('"')
            consumables = row[3].strip('"')
            comparison = row[4].strip('"')
            month = row[5].strip('"')
            try:
                cursor.execute(
                    "INSERT INTO external_records (user_id, month, feature, efficiency, consumables, comparison) VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_id, month, feature, efficiency, consumables, comparison)
                )
            except Exception as e:
                print(f"跳过重复: {user_id}, {month} - {e}")
        conn.commit()
    cursor.close()
    conn.close()
    print("导入完成")

if __name__ == "__main__":
    import_csv_to_mysql()