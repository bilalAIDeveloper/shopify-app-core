DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Shopify Data Dashboard</title>
    <!-- Tailwind CSS for instant clean styling -->
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
        body {{ font-family: 'Inter', sans-serif; background-color: #f7fafc; }}
        .card {{ background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); padding: 20px; }}
    </style>
</head>
<body class="bg-gray-50 min-h-screen p-8">
    <div class="max-w-6xl mx-auto">
        <!-- Header -->
        <header class="flex justify-between items-center mb-8">
            <div>
                <h1 class="text-2xl font-bold text-gray-800">Shopify Data Dashboard</h1>
                <p class="text-gray-500 text-sm mt-1">Status: <span class="text-green-600 font-semibold">● Connected</span></p>
                <div class="text-xs text-gray-400 mt-1">Shop: {shop_domain} | Token: ••••••{masked_token}</div>
            </div>
            <a href="/" class="px-4 py-2 bg-white border border-gray-300 rounded text-sm text-gray-600 hover:bg-gray-50">Back Home</a>
        </header>

        <!-- Stats Grid -->
        <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
            <div class="card border-l-4 border-blue-500">
                <div class="text-gray-500 text-sm font-medium uppercase">Total Products</div>
                <div class="text-3xl font-bold text-gray-800 mt-2">{product_count}</div>
            </div>
            <div class="card border-l-4 border-green-500">
                <div class="text-gray-500 text-sm font-medium uppercase">Total Customers</div>
                <div class="text-3xl font-bold text-gray-800 mt-2">{customer_count}</div>
            </div>
            <div class="card border-l-4 border-purple-500">
                <div class="text-gray-500 text-sm font-medium uppercase">Recent Orders</div>
                <div class="text-3xl font-bold text-gray-800 mt-2">{order_count}</div>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
            <!-- Products Section -->
            <div class="card">
                <h2 class="text-lg font-semibold text-gray-800 mb-4 pb-2 border-b">Recent Products</h2>
                <div class="space-y-3">
                    {products_html}
                </div>
            </div>

            <!-- Customers Section -->
            <div class="card">
                <h2 class="text-lg font-semibold text-gray-800 mb-4 pb-2 border-b">Recent Customers</h2>
                <div class="space-y-3">
                    {customers_html}
                </div>
            </div>
        </div>

        <!-- Orders Section -->
        <div class="card mt-8">
            <h2 class="text-lg font-semibold text-gray-800 mb-4 pb-2 border-b">Latest Orders</h2>
            <div class="overflow-x-auto">
                <table class="min-w-full text-left text-sm">
                    <thead class="bg-gray-50 text-gray-500 font-medium">
                        <tr>
                            <th class="px-4 py-2">Order #</th>
                            <th class="px-4 py-2">Date</th>
                            <th class="px-4 py-2">Customer</th>
                            <th class="px-4 py-2">Total</th>
                            <th class="px-4 py-2">Status</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-100">
                        {orders_html}
                    </tbody>
                </table>
            </div>
        </div>
        
    </div>
</body>
</html>
"""

def generate_product_row(p):
    img = p.get('image', {}).get('src') if p.get('image') else 'https://via.placeholder.com/40'
    return f"""
    <div class="flex items-center space-x-3 p-2 hover:bg-gray-50 rounded transition">
        <img src="{img}" class="w-10 h-10 rounded object-cover border" alt="">
        <div>
            <div class="font-medium text-gray-800">{p.get('title')}</div>
            <div class="text-xs text-gray-500">{p.get('product_type', 'Unknown Type')} | {len(p.get('variants', []))} Variants</div>
        </div>
    </div>
    """

def generate_customer_row(c):
    return f"""
    <div class="flex items-center space-x-3 p-2 hover:bg-gray-50 rounded transition">
        <div class="w-8 h-8 rounded-full bg-green-100 text-green-600 flex items-center justify-center font-bold text-xs">
            {c.get('first_name', '?')[0]}{c.get('last_name', '?')[0]}
        </div>
        <div>
            <div class="font-medium text-gray-800">{c.get('first_name')} {c.get('last_name')}</div>
            <div class="text-xs text-gray-500">{c.get('email')} | {c.get('orders_count', 0)} orders</div>
        </div>
    </div>
    """

def generate_order_row(o):
    return f"""
    <tr class="hover:bg-gray-50">
        <td class="px-4 py-3 font-medium text-gray-800">{o.get('name')}</td>
        <td class="px-4 py-3 text-gray-500">{o.get('created_at', '')[:10]}</td>
        <td class="px-4 py-3 text-gray-600">{o.get('customer', {}).get('first_name', 'Guest')} {o.get('customer', {}).get('last_name', '')}</td>
        <td class="px-4 py-3 font-medium text-gray-800">{o.get('total_price')} {o.get('currency')}</td>
        <td class="px-4 py-3"><span class="px-2 py-1 bg-yellow-100 text-yellow-700 rounded-full text-xs font-medium">{o.get('financial_status')}</span></td>
    </tr>
    """

SEARCH_VISUALIZER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Visual Search Debugger</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
        body { font-family: 'Inter', sans-serif; background-color: #f3f4f6; }
        .result-card { background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); transition: transform 0.2s; }
        .result-card:hover { transform: translateY(-4px); }
        .badge-both { background-color: #fef3c7; color: #92400e; }
        .badge-text { background-color: #dcfce7; color: #166534; }
        .badge-image { background-color: #dbeafe; color: #1e40af; }
    </style>
</head>
<body class="min-h-screen">
    <div class="max-w-7xl mx-auto px-4 py-12">
        <header class="mb-12 text-center">
            <h1 class="text-4xl font-extrabold text-gray-900 mb-4">Visual Search Debugger</h1>
            <p class="text-lg text-gray-600">Test hybrid search across text and image embeddings in real-time.</p>
        </header>

        <section class="max-w-3xl mx-auto bg-white rounded-2xl p-8 shadow-sm mb-12 border border-gray-100">
            <form id="searchForm" class="space-y-6">
                <div>
                    <label class="block text-sm font-semibold text-gray-700 mb-2">Text Query</label>
                    <input type="text" id="queryInput" placeholder="e.g. black cargo pants" 
                           class="w-full px-4 py-3 rounded-lg border border-gray-300 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none transition">
                </div>
                
                <div class="flex items-center space-x-4">
                    <div class="flex-1">
                        <label class="block text-sm font-semibold text-gray-700 mb-2">Image Search (Visual Intent)</label>
                        <input type="file" id="imageInput" accept="image/*" 
                               class="w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-full file:border-0 file:text-sm file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100">
                    </div>
                    <div class="w-32">
                        <label class="block text-sm font-semibold text-gray-700 mb-2">Limit</label>
                        <select id="limitSelect" class="w-full px-3 py-2.5 rounded-lg border border-gray-300">
                            <option value="4">4</option>
                            <option value="8" selected>8</option>
                            <option value="12">12</option>
                            <option value="20">20</option>
                        </select>
                    </div>
                </div>

                <button type="submit" class="w-full bg-blue-600 text-white font-bold py-4 rounded-xl hover:bg-blue-700 transition shadow-lg shadow-blue-200">
                    Execute Hybrid Search
                </button>
            </form>
        </section>

        <div id="loading" class="hidden text-center py-12">
            <div class="inline-block animate-spin rounded-full h-12 w-12 border-4 border-blue-600 border-t-transparent"></div>
            <p class="mt-4 text-gray-500 font-medium">Embedding and searching...</p>
        </div>

        <div id="resultsGrid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-8">
            <!-- Results populated here -->
        </div>
    </div>

    <script>
        document.getElementById('searchForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const query = document.getElementById('queryInput').value;
            const imageFile = document.getElementById('imageInput').files[0];
            const limit = document.getElementById('limitSelect').value;

            const grid = document.getElementById('resultsGrid');
            const loading = document.getElementById('loading');
            grid.innerHTML = '';
            loading.classList.remove('hidden');

            try {
                const formData = new FormData();
                formData.append('query', query);
                formData.append('limit', limit);
                if (imageFile) formData.append('image', imageFile);

                const response = await fetch('/api/search/visualize', {
                    method: 'POST',
                    body: formData
                });

                const data = await response.json();
                loading.classList.add('hidden');

                if (data.results.length === 0) {
                    grid.innerHTML = '<div class="col-span-full text-center py-12 text-gray-500">No products found for this query.</div>';
                    return;
                }

                data.results.forEach(item => {
                    const badgeClass = item._score === 2 ? 'badge-both' : (item._sources.includes('text') ? 'badge-text' : 'badge-image');
                    const badgeText = item._score === 2 ? 'Text + Image' : (item._sources.includes('text') ? 'Text Match' : 'Image Match');
                    
                    const card = `
                        <div class="result-card">
                            <div class="relative aspect-square">
                                <img src="${item.image_url || 'https://via.placeholder.com/300'}" class="w-full h-full object-cover">
                                <div class="absolute top-2 right-2 px-2 py-1 rounded-md text-[10px] font-bold uppercase ${badgeClass} shadow-sm">
                                    ${badgeText}
                                </div>
                            </div>
                            <div class="p-4">
                                <h3 class="font-bold text-gray-900 truncate mb-1">${item.title}</h3>
                                <p class="text-xs text-gray-500 mb-3">${item.type}</p>
                                <div class="flex justify-between items-end">
                                    <span class="text-blue-600 font-bold">PKR ${item.price ? item.price.toLocaleString() : 'N/A'}</span>
                                    <span class="text-[10px] text-gray-400 font-medium">${item.color || 'N/A'} | ${item.size || 'N/A'}</span>
                                </div>
                                <a href="https://ismailsclothing.com/products/${item.handle}" target="_blank" 
                                   class="mt-4 block text-center text-xs font-semibold py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition">
                                    View Product
                                </a>
                            </div>
                        </div>
                    `;
                    grid.insertAdjacentHTML('beforeend', card);
                });

            } catch (err) {
                loading.classList.add('hidden');
                alert('Search failed: ' + err.message);
            }
        });
    </script>
</body>
</html>
"""
