import { getApiUrl, getApiToken } from "../config"; 
//getApiUrl负责拼接接口前缀和相对路径，得到本次请求使用的 URL。

export type UserRole = "admin" | "user";
export type UserStatus = "active" | "disabled";

//用户完整信息：后端返回的用户 JSON 在前端中的类型约定。
export interface UserProfile {
  id: string;
  username: string;
  name: string;
  avatar: string;
  role: UserRole; //引用上面联合类型
  status: UserStatus; //同上
  created_at: string;
  last_login: string;
}


//登录或注册成功后的返回值，其中 token 要保存给后续需要鉴权的请求使用。
export interface LoginResponse {
  token: string; //JWT 令牌，必有，存起来用于后续请求鉴权
  username: string; //登录用户名，必有
  user?: UserProfile | null; //key不存在/key存在但为null/key存在
  message?: string; //后端提示消息，也可能没有
}

//鉴权状态检查：决定登录页是否需要显示，以及是否应引导创建首个用户。
export interface AuthStatusResponse {
  enabled: boolean; //后端未开启认证时，前端跳过登录页直接进入主系统。
  has_users: boolean; //没有已注册用户时，前端自动切换到注册模式创建首个管理员。
}

//注册的可选参数
export interface RegisterOptions {
  name?: string;
  avatar?: string;
}

async function readError(res: Response, fallback: string): Promise<Error> {
  //后端统一用 detail 字段描述错误；解析失败时使用调用者传入的兜底提示。
  const err = await res.json().catch(() => ({}));
  return new Error(err.detail || fallback);
}

//负责把认证相关的前端操作转换成 HTTP 请求。
export const authApi = {
  login: async (username: string, password: string): Promise<LoginResponse> => { //点击登录
    const res = await fetch(getApiUrl("/auth/login"), {
      //fetch 把请求交给浏览器网络层；body 中的对象必须先序列化为 JSON 字符串。
      method: "POST",
      headers: { "Content-Type": "application/json" }, //声明请求体是 JSON，后端才能按 JSON 解析。
      body: JSON.stringify({ username, password }), //把 JavaScript 对象转换成 HTTP 请求体文本。
    });
    if (!res.ok) throw await readError(res, "Login failed");
    //res.ok 为 false 表示 HTTP 状态码不是 2xx，登录页会捕获并展示错误信息。
    return res.json();
  },

  register: async ( //请求流程与登录相同，但额外提交姓名和头像。
    username: string,
    password: string,
    options: RegisterOptions = {},
  ): Promise<LoginResponse> => {
    const res = await fetch(getApiUrl("/auth/register"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        password,
        name: options.name || "",
        avatar: options.avatar || "",
      }),
    });
    if (!res.ok) throw await readError(res, "Registration failed");
    return res.json();
  },

  getStatus: async (): Promise<AuthStatusResponse> => {
    //该接口不需要 token，因为用户尚未登录时也必须能够访问。
    const res = await fetch(getApiUrl("/auth/status"));
    if (!res.ok) throw new Error("Failed to check auth status");
    return res.json();
  },

  getMe: async (): Promise<UserProfile> => { //拿到登录用户的完整信息
    const token = getApiToken(); //从 localStorage 读取登录成功后保存的 token。
    const res = await fetch(getApiUrl("/auth/me"), {
      headers: { Authorization: "Bearer " + token },
    });
    if (!res.ok) throw await readError(res, "Failed to load user profile");
    return res.json();
  },

  updateProfile: async (
    currentPassword: string,
    newUsername?: string,
    newPassword?: string,
    name?: string,
    avatar?: string,
  ): Promise<LoginResponse> => {
    //修改本人资料需要同时证明“当前密码正确”和“当前 token 有效”。
    const token = getApiToken();
    const res = await fetch(getApiUrl("/auth/update-profile"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + token,
      },
      body: JSON.stringify({
        current_password: currentPassword,
        new_username: newUsername || null,
        new_password: newPassword || null,
        name: name === undefined ? null : name,
        avatar: avatar === undefined ? null : avatar,
      }),
    });
    if (!res.ok) throw await readError(res, "Update failed");
    return res.json();
  },

  logout: async (): Promise<void> => {
    const token = getApiToken();
    if (!token) return;
    //通知后端撤销当前 token，使其不能继续被用于访问受保护接口。
    const res = await fetch(getApiUrl("/auth/revoke-token"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + token,
      },
      body: JSON.stringify({}),
    });
    if (!res.ok) throw await readError(res, "Logout failed");
  },
};
