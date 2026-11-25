/** 徐州招商工作台依赖的 QwenPaw Console 最小宿主契约。 */

declare global {
  interface QwenPawHost {
    /** Console 注入的 React 实例；插件不得自行打包第二份 React。 */
    React: any;
    /** Console 注入的 Ant Design 组件集合。 */
    antd: any;
    /** 将插件相对 API 路径解析为当前 Console 的完整 API 地址。 */
    getApiUrl: (path: string) => string;
    /** 返回当前登录会话的短期令牌；身份仍由服务器验证。 */
    getApiToken: () => string;
  }

  interface QwenPawRoute {
    path: string;
    component: unknown;
    label?: string;
    icon?: string;
    priority?: number;
  }

  interface QwenPawGlobal {
    host: QwenPawHost;
    registerRoutes?: (pluginId: string, routes: QwenPawRoute[]) => void;
  }

  interface Window {
    QwenPaw: QwenPawGlobal;
  }
}

export {};
