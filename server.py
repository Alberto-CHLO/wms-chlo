from flask import Flask, jsonify, request
from commands.views.admin import (
    warehouse_home_display_query,
    warehouse_display_prodcuts_query,
    product_receipt_detail_query,
    create_purchase_order_query
)
from commands.views.warehouse import (
    warehouse_fill_capacity_query,
)
import sqlite3, json
app = Flask(__name__)
DATABASE = 'wms.db'

def execute_query(query, params=()):
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

# ------------------------------------------------------------- ADMINISTRATIVE
# ------------------------------------------------ GET
@app.route("/v1/administrative/home/", methods=["GET"])
def warehouse_home_display():
    warehouses = execute_query(warehouse_home_display_query)
    return jsonify(warehouses)

@app.route("/v1/administrative/warehouse/products/", methods=["GET"])
def warehouse_products():
    warehouse_id = request.args.get('warehouse_id')
    if not warehouse_id:
        return jsonify({"error": "warehouse_id query parameter is required"}), 400

    # Check if warehouse exists and get its data
    warehouse_query = "SELECT warehouse_name FROM Warehouse WHERE id = ?"
    warehouse_result = execute_query(warehouse_query, (warehouse_id,))
    if not warehouse_result:
        return jsonify({"error": "Warehouse not found"}), 404

    # Get inventory items with product details and max capacity
    products = execute_query(warehouse_display_prodcuts_query, (warehouse_id,))
    
    return jsonify({
        "warehouse_name": warehouse_result[0]['warehouse_name'],
        "products": products
    })

@app.route("/v1/administrative/product_receipt/detail/", methods=["GET"])
def product_receipt_detail():
    receipt_id = request.args.get('receipt_id')
    if not receipt_id:
        return jsonify({"error": "receipt_id query parameter is required"}), 400
    
    receipt = execute_query(product_receipt_detail_query, (receipt_id,))
    if not receipt:
        return jsonify({"error": "Product receipt not found"}), 404

    # Parse the JSON string of items back into a list
    receipt_data = receipt[0]
    receipt_data['items'] = json.loads(receipt_data['items'])
    
    return jsonify(receipt_data)

# ------------------------------------------------ POST
@app.route("/v1/administrative/purchase_order/create/", methods=["POST"])
def create_purchase_order():
    data = request.get_json()
    
    # Validate required fields
    supplier_id = data.get('supplier_id')
    total_amount = data.get('total_amount')
    
    if not supplier_id or not total_amount:
        return jsonify({
            'error': 'Supplier ID and Total amount are required'
        }), 400

    # Check if supplier exists
    supplier_query = "SELECT id FROM Supplier WHERE id = ?"
    supplier = execute_query(supplier_query, (supplier_id,))
    if not supplier:
        return jsonify({'error': 'Supplier not found'}), 404

    # Create purchase order (always using warehouse ID 1 as per serializer logic)
    try:
        purchase_order = execute_query(create_purchase_order_query, (supplier_id, total_amount))
        return jsonify(purchase_order[0]), 201
    except sqlite3.Error as e:
        return jsonify({'error': str(e)}), 400

# ------------------------------------------------------------- WAREHOUSE
# ------------------------------------------------ GET
@app.route("/v1/warehouse/fill_capacity/", methods=["GET"])
def warehouse_fill_capacity():
    warehouse_id = request.args.get('warehouse_id')
    if not warehouse_id:
        return jsonify({"error": "warehouse_id query parameter is required"}), 400

    # Check if warehouse exists
    warehouse_query = "SELECT id FROM Warehouse WHERE id = ?"
    warehouse = execute_query(warehouse_query, (warehouse_id,))
    if not warehouse:
        return jsonify({"error": "Warehouse not found"}), 404

    # Get products that need filling
    products = execute_query(warehouse_fill_capacity_query, (warehouse_id,))
    return jsonify(products)

# ------------------------------------------------ PUT
@app.route("/v1/warehouse/transfer/", methods=["PUT"])
def product_transfer():
    data = request.get_json()
    
    # Validate required fields
    required_fields = ['from_warehouse', 'to_warehouse', 'products_to_get']
    if not all(field in data for field in required_fields):
        return jsonify({'error': 'Missing required fields','required': required_fields}), 400

    if data['from_warehouse'] == data['to_warehouse']:
        return jsonify({'error': 'Source and destination warehouses must be different'}), 400

    # Validate warehouses exist
    warehouses = execute_query(
        "SELECT id, warehouse_name FROM Warehouse WHERE id IN (?, ?)", 
        (data['from_warehouse'], data['to_warehouse'])
    )
    
    if len(warehouses) != 2:
        return jsonify({'error': 'One or both warehouses do not exist'}), 404

    # Create a dictionary to map warehouse IDs to names
    warehouse_names = {str(w['id']): w['warehouse_name'] for w in warehouses}
    transfers = []

    try:
        for product_item in data['products_to_get']:
            product_id = product_item['product_id']
            quantity_needed = product_item['quantity_needed']

            if quantity_needed <= 0:
                return jsonify({'error': f'Invalid quantity for product {product_id}. Must be greater than 0'}), 400

            # Check if product exists and get source inventory
            source_inventory = execute_query("""
                SELECT i.quantity, p.product_name 
                FROM Inventory i
                JOIN Product p ON p.id = i.product_id
                WHERE i.warehouse_id = ? AND i.product_id = ?
            """, (data['from_warehouse'], product_id))

            if not source_inventory:
                return jsonify({'error': f'Product {product_id} not found in source warehouse'}), 404

            available_quantity = source_inventory[0]['quantity']
            if available_quantity < quantity_needed:
                return jsonify({'error': f'Insufficient inventory for product {product_id}','available': available_quantity,'requested': quantity_needed}), 400

            # Update source warehouse inventory
            execute_query("""
                UPDATE Inventory
                SET quantity = quantity - ?
                WHERE warehouse_id = ? AND product_id = ?
            """, (quantity_needed, data['from_warehouse'], product_id))

            # Update or insert destination warehouse inventory
            dest_inventory = execute_query("""
                INSERT INTO Inventory (warehouse_id, product_id, quantity, status)
                VALUES (?, ?, ?, 'GOOD_CAPACITY')
                ON CONFLICT(warehouse_id, product_id) 
                DO UPDATE SET quantity = quantity + ?
                RETURNING id
            """, (data['to_warehouse'], product_id, quantity_needed, quantity_needed))

            # Update inventory status for both warehouses
            for warehouse_id in [data['from_warehouse'], data['to_warehouse']]:
                # Get current quantity and capacity data
                inventory_data = execute_query("""
                    SELECT i.quantity, wc.max_capacity, wc.capacity_percentage
                    FROM Inventory i
                    JOIN WarehouseCapacity wc ON wc.warehouse_id = i.warehouse_id 
                        AND wc.product_id = i.product_id
                    WHERE i.warehouse_id = ? AND i.product_id = ?
                """, (warehouse_id, product_id))

                if inventory_data:
                    current_qty = inventory_data[0]['quantity']
                    max_capacity = inventory_data[0]['max_capacity']
                    threshold = inventory_data[0]['capacity_percentage']
                    
                    # Calculate and update status
                    capacity_percentage = (current_qty / max_capacity) * 100
                    new_status = 'GOOD_CAPACITY' if capacity_percentage > threshold else 'LOW_CAPACITY'
                    
                    execute_query("""
                        UPDATE Inventory
                        SET status = ?
                        WHERE warehouse_id = ? AND product_id = ?
                    """, (new_status, warehouse_id, product_id))

            transfers.append({
                'product_id': product_id,
                'product_name': source_inventory[0]['product_name'],
                'quantity': quantity_needed,
                'from_warehouse': warehouse_names[str(data['from_warehouse'])],
                'to_warehouse': warehouse_names[str(data['to_warehouse'])]
            })

        return jsonify({
            'status': 'success',
            'message': 'Products transferred successfully',
            'transfers': transfers
        })

    except sqlite3.Error as e:
        return jsonify({'error': str(e)}), 500

    
if __name__ == "__main__":
    app.run(debug=True)