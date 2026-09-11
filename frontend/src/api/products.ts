import { apiFetch, buildQuery } from './client'

export interface Product {
  id: number
  store_id: number
  sku: string
  name: string
  description: string | null
  category_id: number | null
  default_supplier_id: number | null
  unit_of_measure: string
  is_weighed: boolean
  current_price: string
  current_cost: string
  tax_rate_id: number | null
  reorder_point: string | null
  current_qty_on_hand: string
  allow_negative_stock: boolean
  is_active: boolean
}

export interface ProductCreateInput {
  store_id: number
  sku: string
  name: string
  description?: string
  category_id?: number | null
  current_price: string
  tax_rate_id?: number | null
  reorder_point?: string | null
  allow_negative_stock?: boolean
}

export type ProductUpdateInput = Partial<Omit<ProductCreateInput, 'store_id' | 'sku'>>

export interface ProductListParams {
  store_id?: number
  search?: string
  is_active?: boolean
}

export function listProducts(params: ProductListParams = {}): Promise<Product[]> {
  return apiFetch<Product[]>(`/api/v1/products${buildQuery(params)}`)
}

export function getProductByBarcode(barcode: string): Promise<Product> {
  return apiFetch<Product>(`/api/v1/products/barcode/${encodeURIComponent(barcode)}`)
}

export function createProduct(input: ProductCreateInput): Promise<Product> {
  return apiFetch<Product>('/api/v1/products', { method: 'POST', body: JSON.stringify(input) })
}

export function updateProduct(id: number, input: ProductUpdateInput): Promise<Product> {
  return apiFetch<Product>(`/api/v1/products/${id}`, {
    method: 'PUT',
    body: JSON.stringify(input),
  })
}

export function setProductActive(id: number, active: boolean): Promise<Product> {
  return apiFetch<Product>(`/api/v1/products/${id}/${active ? 'activate' : 'deactivate'}`, {
    method: 'POST',
  })
}

export function addBarcode(
  productId: number,
  barcode: string,
  isPrimary = false,
): Promise<{ id: number; barcode: string }> {
  return apiFetch(`/api/v1/products/${productId}/barcodes`, {
    method: 'POST',
    body: JSON.stringify({ barcode, is_primary: isPrimary }),
  })
}
