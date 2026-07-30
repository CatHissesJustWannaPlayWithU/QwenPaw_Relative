import { create } from "zustand";
import type { UserProfile } from "../api/modules/auth";
import { authApi } from "../api/modules/auth";
import { menuRegistry } from "../plugins/registry/store";

//跨组件共享的登录用户状态；Header、路由守卫和菜单都从这里读取当前用户。
interface AuthState {
  user: UserProfile | null;
  loading: boolean;
  setUser: (user: UserProfile | null) => void;
  refresh: () => Promise<UserProfile | null>;
  clear: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  loading: false,
  setUser: (user) => {
    set({ user });
    //菜单可见性依赖 user.role，更新用户后通知菜单注册表重新计算。
    menuRegistry.refresh();
  },
  refresh: async () => {
    set({ loading: true });
    try {
      //刷新浏览器后内存状态会丢失，因此重新请求 /auth/me 恢复完整用户资料。
      const user = await authApi.getMe();
      set({ user, loading: false });
      menuRegistry.refresh();
      return user;
    } catch {
      set({ user: null, loading: false });
      menuRegistry.refresh();
      return null;
    }
  },
  clear: () => {
    //退出登录或 token 失效时同时清空用户资料，避免界面继续显示旧头像和管理员菜单。
    set({ user: null, loading: false });
    menuRegistry.refresh();
  },
}));
