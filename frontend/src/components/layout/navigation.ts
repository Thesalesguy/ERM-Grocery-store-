export interface NavItem {
  label: string
  path: string
  /** Hidden from the nav (and inaccessible) unless the current user has
   * this permission. Omitted for pages open to any authenticated user. */
  permission?: string
}

/**
 * Single source of truth for the sidebar nav and the route table in
 * App.tsx, so adding a module's real page later means updating one place.
 */
export const NAV_ITEMS: NavItem[] = [
  { label: 'Dashboard', path: '/' },
  { label: 'POS', path: '/pos', permission: 'pos.use' },
  { label: 'Products', path: '/products', permission: 'products.read' },
  { label: 'Inventory', path: '/inventory', permission: 'inventory.read' },
  { label: 'Purchasing', path: '/purchasing', permission: 'purchasing.read' },
  { label: 'Suppliers', path: '/suppliers', permission: 'purchasing.read' },
  { label: 'Sales', path: '/sales', permission: 'sales.read' },
  { label: 'Accounting', path: '/accounting', permission: 'accounting.read' },
  { label: 'Reports', path: '/reports', permission: 'reports.read' },
  { label: 'Users', path: '/users', permission: 'users.manage' },
  { label: 'Settings', path: '/settings' },
]
