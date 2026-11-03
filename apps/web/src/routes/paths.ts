// ----------------------------------------------------------------------

export const paths = {
  home: '/',
  login: '/login',
  setup: '/setup',
  settings: '/settings',
  users: '/users',
  versions: '/versions',
  jobs: '/jobs',
  audit: '/audit',
  page404: '/error/404',
};

/**
 * 邀请页的路由模式。链接由 `invitePath` 从同一个常量派生，
 * 避免「路由写了 /invite/:token、链接写成别的拼法」这类漂移。
 */
export const inviteRoute = '/invite/:token';

export function invitePath(token: string): string {
  return inviteRoute.replace(':token', encodeURIComponent(token));
}
