export interface NavItem {
  label: string
  path: string
}

/**
 * Single source of truth for the sidebar nav and the route table in
 * App.tsx, so adding a module's real page later means updating one place.
 */
export const NAV_ITEMS: NavItem[] = [
  { label: 'Dashboard', path: '/' },
  { label: 'POS', path: '/pos' },
  { label: 'Products', path: '/products' },
  { label: 'Inventory', path: '/inventory' },
  { label: 'Purchasing', path: '/purchasing' },
  { label: 'Suppliers', path: '/suppliers' },
  { label: 'Sales', path: '/sales' },
  { label: 'Reports', path: '/reports' },
  { label: 'Users', path: '/users' },
  { label: 'Settings', path: '/settings' },
]
