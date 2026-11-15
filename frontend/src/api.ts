import type {
  BasePotentialRow,
  BulkRequest,
  BulkResult,
  Category,
  CurrentUser,
  JournalBatchPage,
  JournalPage,
  JournalQuery,
  Matrix,
  MatrixFilters,
  MatrixImport,
  Order,
  OrderDraft,
  OrderStatus,
  Pair,
  PriceCard,
  Product,
  PurchasePlan,
  Role,
  SeasonalityRow,
  Store,
  Supplier,
  User,
} from './types';

const BASE = '/api/v1';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * Вызывается, когда сессия перестала действовать: сервер ответил 401.
 * Приложение подписывается на это и возвращает пользователя на экран входа.
 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

/** Пути, на которых 401 — нормальный ответ, а не потеря сессии. */
const AUTH_PATHS = ['/auth/login', '/auth/me'];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!response.ok) {
    const text = await response.text();
    let detail = text.slice(0, 300);
    try {
      detail = (JSON.parse(text).detail as string) ?? detail;
    } catch {
      // Ответ не JSON — оставляем как есть.
    }
    if (response.status === 401 && !AUTH_PATHS.some((p) => path.startsWith(p))) {
      onUnauthorized?.();
    }
    throw new ApiError(response.status, detail);
  }
  // Удаление отвечает 204 без тела: разбирать там нечего, а `json()` на пустом
  // ответе бросает исключение — успешный запрос выглядел бы ошибкой.
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function query(filters: Partial<MatrixFilters>): string {
  const params = new URLSearchParams();
  if (filters.level) params.set('level', filters.level);
  if (filters.storeId) params.set('store_id', String(filters.storeId));
  if (filters.categoryId) params.set('category_id', String(filters.categoryId));
  if (filters.search?.trim()) params.set('search', filters.search.trim());
  if (filters.rowStatus) params.set('status', filters.rowStatus);
  return params.toString();
}

function journalQuery(query: JournalQuery, limit: number, offset: number): string {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (query.storeId) params.set('store_id', String(query.storeId));
  if (query.categoryId) params.set('category_id', String(query.categoryId));
  if (query.search?.trim()) params.set('search', query.search.trim());
  if (query.field) params.set('field', query.field);
  if (query.author?.trim()) params.set('author', query.author.trim());
  if (query.batchId) params.set('batch_id', query.batchId);
  return params.toString();
}

export const api = {
  me: () => request<CurrentUser>('/auth/me'),

  login: (email: string, password: string) =>
    request<CurrentUser>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  logout: () => request<unknown>('/auth/logout', { method: 'POST' }),

  // Смена своего пароля. Сервер требует прежний пароль и в ответ выдаёт новую
  // куку: эта вкладка продолжает работать, остальные сессии закрываются.
  changeMyPassword: (currentPassword: string, newPassword: string) =>
    request<CurrentUser>('/auth/me/password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),

  // Управление пользователями. Ручки закрыты ролью на сервере: вкладка, спрятанная
  // от не-администратора, — удобство, а не защита.
  users: () => request<User[]>('/auth/users'),

  createUser: (payload: { email: string; full_name: string; role: Role; password: string }) =>
    request<User>('/auth/users', { method: 'POST', body: JSON.stringify(payload) }),

  updateUser: (
    userId: number,
    payload: { full_name?: string; role?: Role; is_active?: boolean; password?: string },
  ) => request<User>(`/auth/users/${userId}`, { method: 'PUT', body: JSON.stringify(payload) }),

  stores: () => request<Store[]>('/catalog/stores'),
  categories: () => request<Category[]>('/catalog/categories'),

  products: (search?: string) =>
    request<Product[]>(
      `/catalog/products${search?.trim() ? `?search=${encodeURIComponent(search.trim())}` : ''}`,
    ),

  // История цен (задача B-9). Любое изменение истории возвращает карточку
  // целиком: правка задним числом может не сдвинуть действующую цену, и экран
  // не должен догадываться об этом сам.
  prices: (productId: number) => request<PriceCard>(`/catalog/products/${productId}/prices`),

  addPrice: (
    productId: number,
    payload: { valid_from: string; cost_price?: number; price?: number; note?: string },
  ) =>
    request<PriceCard>(`/catalog/products/${productId}/prices`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  deletePrice: (productId: number, priceId: number) =>
    request<PriceCard>(`/catalog/products/${productId}/prices/${priceId}`, { method: 'DELETE' }),

  // Ведение справочников (задача B-14). Цен здесь нет: в действующую цену пишет
  // только сервис цен, иначе карточка и история разойдутся. Удаление отвечает
  // 204 и отказывает (422), если на запись ссылаются факты, заказы или товары —
  // такую запись отключают, а не удаляют.
  suppliers: () => request<Supplier[]>('/catalog/suppliers'),

  createStore: (payload: { code: string; name: string; marketplace?: string }) =>
    request<Store>('/catalog/stores', { method: 'POST', body: JSON.stringify(payload) }),

  updateStore: (storeId: number, payload: { name?: string; is_active?: boolean }) =>
    request<Store>(`/catalog/stores/${storeId}`, { method: 'PUT', body: JSON.stringify(payload) }),

  deleteStore: (storeId: number) =>
    request<void>(`/catalog/stores/${storeId}`, { method: 'DELETE' }),

  createSupplier: (payload: { name: string; lead_time_days?: number; moq?: number }) =>
    request<Supplier>('/catalog/suppliers', { method: 'POST', body: JSON.stringify(payload) }),

  updateSupplier: (
    supplierId: number,
    payload: { name?: string; lead_time_days?: number; moq?: number },
  ) =>
    request<Supplier>(`/catalog/suppliers/${supplierId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  deleteSupplier: (supplierId: number) =>
    request<void>(`/catalog/suppliers/${supplierId}`, { method: 'DELETE' }),

  createCategory: (payload: { name: string; niche?: string | null; abc_class?: string | null }) =>
    request<Category>('/catalog/categories', { method: 'POST', body: JSON.stringify(payload) }),

  updateCategory: (
    categoryId: number,
    payload: { name?: string; niche?: string | null; abc_class?: string | null },
  ) =>
    request<Category>(`/catalog/categories/${categoryId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  deleteCategory: (categoryId: number) =>
    request<void>(`/catalog/categories/${categoryId}`, { method: 'DELETE' }),

  createProduct: (payload: {
    vendor_code: string;
    title: string;
    category_id?: number | null;
    supplier_id?: number | null;
    volume_m3?: number;
    lead_time_days?: number | null;
    moq?: number | null;
    pack_size?: number | null;
    cost_price?: number | null;
    price?: number | null;
  }) => request<Product>('/catalog/products', { method: 'POST', body: JSON.stringify(payload) }),

  updateProduct: (
    productId: number,
    payload: {
      vendor_code?: string;
      title?: string;
      category_id?: number | null;
      supplier_id?: number | null;
      volume_m3?: number;
      lead_time_days?: number | null;
      moq?: number | null;
      pack_size?: number | null;
      is_active?: boolean;
      clear?: string[];
    },
  ) =>
    request<Product>(`/catalog/products/${productId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  deleteProduct: (productId: number) =>
    request<void>(`/catalog/products/${productId}`, { method: 'DELETE' }),

  // Пары товара: ответ на добавление — весь список, потому что экран показывает
  // его целиком, а не только добавленную строку.
  productStores: (productId: number) =>
    request<Pair[]>(`/catalog/products/${productId}/stores`),

  addProductStore: (
    productId: number,
    payload: { store_id: number; nm_id?: string | null; base_potential?: number },
  ) =>
    request<Pair[]>(`/catalog/products/${productId}/stores`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updatePair: (
    pairId: number,
    payload: { nm_id?: string | null; base_potential?: number; is_active?: boolean },
  ) => request<Pair>(`/catalog/pairs/${pairId}`, { method: 'PUT', body: JSON.stringify(payload) }),

  deletePair: (pairId: number) => request<void>(`/catalog/pairs/${pairId}`, { method: 'DELETE' }),

  /**
   * Матрица с окном строк. Итоги, KPI и счётчики в ответе — по всей выборке,
   * режутся только строки; `window` пусто — вся выборка, как для выгрузки.
   */
  matrix: (filters: MatrixFilters, window?: { offset?: number; limit?: number }) => {
    const params = new URLSearchParams(query(filters));
    if (window?.offset) params.set('offset', String(window.offset));
    if (window?.limit) params.set('limit', String(window.limit));
    return request<Matrix>(`/matrix?${params.toString()}`);
  },

  /**
   * Одна строка матрицы — ответ на правку ячейки.
   *
   * Формат тот же, что у матрицы, но строк в нём не больше одной; итоги, KPI
   * и счётчики — по всей выборке, как обычно. Пустой список строк означает, что
   * позиция из выборки выпала: правка изменила её статус, а на экране включён
   * отбор по статусу. Тогда экран перечитывает выборку целиком.
   */
  matrixRow: (filters: MatrixFilters, pairId: number) =>
    request<Matrix>(`/matrix/row?pair_id=${pairId}&${query(filters)}`),

  purchase: (filters: Omit<MatrixFilters, 'level'>, onlyNeeded: boolean) =>
    request<PurchasePlan>(`/purchase?only_needed=${onlyNeeded}&${query(filters)}`),

  // Выгрузки — обычные ссылки, а не fetch: файл сохраняет браузер, с именем,
  // которое прислал сервер. Фильтры те же, что у запроса данных, иначе в файле
  // окажется не та выборка, которую человек видит на экране.
  matrixExportUrl: (filters: MatrixFilters) => `${BASE}/matrix/export?${query(filters)}`,

  purchaseExportUrl: (filters: Omit<MatrixFilters, 'level'>, onlyNeeded: boolean) =>
    `${BASE}/purchase/export?only_needed=${onlyNeeded}&${query(filters)}`,

  // Обратная дорога: файл уходит на сервер как есть. Сначала с `apply=false` —
  // это только разбор, в базу ничего не пишется; человек смотрит, что изменится,
  // и тот же файл уходит второй раз уже с подтверждением.
  importMatrix: (file: File, apply: boolean) => {
    const body = new FormData();
    body.append('file', file);
    // Content-Type не ставим руками: его с границей формы проставляет браузер.
    return request<MatrixImport>(`/matrix/import?apply=${apply}`, {
      method: 'POST',
      headers: {},
      body,
    });
  },

  setForecast: (
    pairId: number,
    week: string,
    payload: {
      manual_forecast?: number;
      manual_adapted?: number;
      diff_percent?: number;
      clear?: string[];
    },
  ) =>
    request<unknown>(`/forecast/${pairId}/${week}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  bulkAdjust: (payload: BulkRequest) =>
    request<BulkResult>('/forecast/bulk', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  basePotential: (filters: Omit<MatrixFilters, 'level'>, factWeeks: number) =>
    request<BasePotentialRow[]>(
      `/reference/base-potential?fact_weeks=${factWeeks}&${query(filters)}`,
    ),

  setBasePotential: (pairId: number, value: number) =>
    request<BasePotentialRow>(`/reference/base-potential/${pairId}`, {
      method: 'PUT',
      body: JSON.stringify({ base_potential: value }),
    }),

  journalEntries: (query: JournalQuery, limit: number, offset: number) =>
    request<JournalPage>(`/forecast/log?${journalQuery(query, limit, offset)}`),

  journalBatches: (query: JournalQuery, limit: number, offset: number) =>
    request<JournalBatchPage>(`/forecast/log/batches?${journalQuery(query, limit, offset)}`),

  seasonality: () => request<SeasonalityRow[]>('/reference/seasonality'),

  setSeasonality: (categoryId: number, coefficients: Record<number, number>) =>
    request<SeasonalityRow>(`/reference/seasonality/${categoryId}`, {
      method: 'PUT',
      body: JSON.stringify({ coefficients }),
    }),

  orders: (status?: OrderStatus) =>
    request<Order[]>(`/orders${status ? `?status=${status}` : ''}`),

  createOrder: (draft: OrderDraft) =>
    request<Order>('/orders', { method: 'POST', body: JSON.stringify(draft) }),

  // Правка черновика. Сервер отвечает заказом целиком, но экран всё равно
  // перечитывает список: правка меняет итоги, а они видны и в соседних строках.
  updateOrderLine: (
    orderId: number,
    lineId: number,
    payload: { qty?: number; arrival_week?: string },
  ) =>
    request<Order>(`/orders/${orderId}/lines/${lineId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  addOrderLine: (
    orderId: number,
    line: { pair_id: number; qty: number; arrival_week: string },
  ) =>
    request<Order>(`/orders/${orderId}/lines`, {
      method: 'POST',
      body: JSON.stringify(line),
    }),

  deleteOrderLine: (orderId: number, lineId: number) =>
    request<Order>(`/orders/${orderId}/lines/${lineId}`, { method: 'DELETE' }),

  setOrderNote: (orderId: number, note: string | null) =>
    request<Order>(`/orders/${orderId}`, { method: 'PATCH', body: JSON.stringify({ note }) }),

  setOrderStatus: (orderId: number, status: OrderStatus) =>
    request<Order>(`/orders/${orderId}/status`, {
      method: 'POST',
      body: JSON.stringify({ status }),
    }),

  syncWb: () => request<unknown>('/sync/wb', { method: 'POST' }),
};
