import { request } from "../request";
import type { UserProfile, UserRole, UserStatus } from "./auth";

//管理员查询用户列表时的响应结构。
export interface UserListResponse {
  users: UserProfile[];
}

//新增用户时必须一次性给出的字段，密码只会在创建阶段必填。
export interface CreateUserPayload {
  username: string;
  password: string;
  name: string;
  avatar?: string;
  role: UserRole;
  status: UserStatus;
}

//修改用户时所有字段都可选，只提交实际需要变更的字段。
export interface UpdateUserPayload {
  username?: string;
  password?: string;
  name?: string;
  avatar?: string;
  role?: UserRole;
  status?: UserStatus;
}

//管理员用户管理接口。request 会自动附加 Bearer token，并在收到 401 时统一跳回登录页。
export const usersApi = {
  //查询当前所有用户，用于初始化用户管理表格。
  list: () => request<UserListResponse>("/users"),
  //把新增弹窗中的表单数据作为 JSON 发送给后端。
  create: (payload: CreateUserPayload) =>
    request<UserProfile>("/users", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  //encodeURIComponent 防止用户 ID 中的特殊字符破坏 URL 路径。
  update: (userId: string, payload: UpdateUserPayload) =>
    request<UserProfile>("/users/" + encodeURIComponent(userId), {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  remove: (userId: string) =>
    request<{ deleted: boolean; id: string }>(
      "/users/" + encodeURIComponent(userId),
      { method: "DELETE" },
    ),
};
