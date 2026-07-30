import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button, Form, Input, Upload } from "antd";
import type { UploadFile } from "antd";
import { LockOutlined, UploadOutlined, UserOutlined } from "@ant-design/icons";
import { useAppMessage } from "../../hooks/useAppMessage";
import { authApi } from "../../api/modules/auth";
import { setAuthToken } from "../../api/config";
import { useTheme } from "../../contexts/ThemeContext";

//登录与注册表单提交时需要读取的字段；确认密码只在前端注册模式中使用。
interface LoginFormValues {
  username: string; 
  password: string; 
  confirmPassword?: string; 
  name?: string;
}

//将图片从File对象转成Base64字符串，以便在<img src="..."> 里预览，或者传给后端。
function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => { 
    const reader = new FileReader(); //浏览器原生文件读取器
    reader.onload = () => resolve(String(reader.result || "")); 
    //onload是FileReader的回调，reader.result就是读出来的Base64字符串
    reader.onerror = () => reject(new Error("Failed to read avatar")); 
    //读失败了
    reader.readAsDataURL(file);
  });
}

export default function LoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { isDark } = useTheme();
  const [loading, setLoading] = useState(false); //防止按钮重复提交。
  const [isRegister, setIsRegister] = useState(false); //控制同一页面显示登录或注册表单。
  const [hasUsers, setHasUsers] = useState(true); //用于识别是否是创建首个用户的场景。
  const [avatar, setAvatar] = useState(""); //保存头像的 Data URL，注册时作为 JSON 字段发送。
  const [avatarFileList, setAvatarFileList] = useState<UploadFile[]>([]);
  const { message } = useAppMessage();

  useEffect(() => {
    //页面首次加载时先询问后端认证是否开启、系统是否已有用户。
    authApi
      .getStatus()
      .then((res) => {
        if (!res.enabled) {
          navigate("/chat", { replace: true });
          return;
        }
        setHasUsers(res.has_users);
        if (!res.has_users) {
          setIsRegister(true);
        }
      })
      .catch(() => {});
  }, [navigate]);

  const onFinish = async (values: LoginFormValues) => {
    setLoading(true);
    try {
      const raw = searchParams.get("redirect") || "/chat";
      //只接受本站以单个 / 开头的地址，避免登录成功后被重定向到外部网站。
      const redirect =
        raw.startsWith("/") && !raw.startsWith("//") ? raw : "/chat";

      if (isRegister) {
        //确认密码只用于减少输入错误；密码规则和角色分配仍由后端最终校验。
        if (values.password !== values.confirmPassword) {
          message.error(t("login.passwordMismatch", "两次输入的密码不一致"));
          return;
        }
        const res = await authApi.register(values.username, values.password, {
          name: values.name,
          avatar,
        });
        if (res.token) {
          //注册成功会直接获得登录 token，因此不需要用户再输入一次密码登录。
          setAuthToken(res.token);
          message.success(t("login.registerSuccess"));
          navigate(redirect, { replace: true });
        }
      } else {
        const res = await authApi.login(values.username, values.password);
        if (res.token) {
          //token 写入 localStorage 后，跳转到主系统时 AuthGuard 会据此恢复用户资料。
          setAuthToken(res.token);
          navigate(redirect, { replace: true });
        } else {
          message.info(t("login.authNotEnabled"));
          navigate(redirect, { replace: true });
        }
      }
    } catch (err) {
      const errorMsg =
        err instanceof Error
          ? err.message
          : isRegister
            ? t("login.registerFailed")
            : t("login.failed");
      message.error(errorMsg);
    } finally {
      setLoading(false);
    }
  };

  const switchMode = (register: boolean) => {
    //切换模式时丢弃仅属于注册表单的头像选择，避免误带入下一次提交。
    setIsRegister(register);
    setAvatar("");
    setAvatarFileList([]);
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        background: isDark ? "#171717" : "#edf2fb",
      }}
    >
      <div
        style={{
          width: "min(100%, 420px)",
          padding: "36px 36px 30px",
          borderRadius: 16,
          background: isDark ? "#242424" : "#fff",
          boxShadow: isDark
            ? "0 18px 45px rgba(0,0,0,0.32)"
            : "0 18px 45px rgba(40, 67, 105, 0.14)",
        }}
      >
        <div style={{ textAlign: "center", marginBottom: 28 }}>
          <img
            src={isDark ? "/logo-dark.png" : "/logo-light.png"}
            alt="QwenPaw"
            style={{ height: 48, marginBottom: 12 }}
          />
          <h2 style={{ margin: 0, fontWeight: 600, fontSize: 22 }}>
            {isRegister
              ? t("login.registerTitle")
              : t("login.title")}
          </h2>
          <p
            style={{
              margin: "8px 0 0",
              color: isDark ? "rgba(255,255,255,0.52)" : "#7c8798",
              fontSize: 13,
            }}
          >
            {hasUsers
              ? t("login.subtitle", "欢迎使用 QwenPaw")
              : t("login.firstUserHint")}
          </p>
        </div>

        <Form
          layout="vertical"
          onFinish={onFinish}
          autoComplete="off"
          size="large"
        >
          {isRegister && (
            <>
              <Form.Item
                name="name"
                rules={[
                  {
                    required: true,
                    message: t("login.nameRequired", "请输入姓名"),
                  },
                ]}
              >
                <Input
                  prefix={<UserOutlined />}
                  placeholder={t("login.namePlaceholder", "姓名")}
                />
              </Form.Item>
              <Form.Item label={t("login.avatar", "头像")}>
                <Upload
                  accept="image/*"
                  maxCount={1}
                  fileList={avatarFileList}
                  beforeUpload={async (file) => {
                    //不把文件交给 Upload 组件自动上传，而是转成 Data URL 并随注册 JSON 一起提交。
                    const dataUrl = await readFileAsDataUrl(file);
                    setAvatar(dataUrl);
                    setAvatarFileList([file]);
                    return false;
                  }}
                  onRemove={() => {
                    setAvatar("");
                    setAvatarFileList([]);
                  }}
                  showUploadList={{ showPreviewIcon: false }}
                >
                  <Button icon={<UploadOutlined />}>
                    {t("login.chooseAvatar", "选择头像")}
                  </Button>
                </Upload>
              </Form.Item>
            </>
          )}

          <Form.Item
            name="username"
            rules={[{ required: true, message: t("login.usernameRequired") }]}
          >
            <Input
              prefix={<UserOutlined />}
              placeholder={t("login.usernamePlaceholder")}
              autoFocus
            />
          </Form.Item>

          <Form.Item
            name="password"
            rules={[
              { required: true, message: t("login.passwordRequired") },
              ...(isRegister
                ? [
                    {
                      min: 8,
                      message: t(
                        "login.passwordMinLength",
                        "密码至少需要 8 位",
                      ),
                    },
                  ]
                : []),
            ]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder={t("login.passwordPlaceholder")}
            />
          </Form.Item>

          {isRegister && (
            <Form.Item
              name="confirmPassword"
              dependencies={["password"]}
              rules={[
                {
                  required: true,
                  message: t("login.confirmPasswordRequired", "请确认密码"),
                },
              ]}
            >
              <Input.Password
                prefix={<LockOutlined />}
                placeholder={t(
                  "login.confirmPasswordPlaceholder",
                  "确认密码",
                )}
              />
            </Form.Item>
          )}

          <Form.Item style={{ marginBottom: 10, marginTop: 8 }}>
            <Button
              type="primary"
              htmlType="submit"
              loading={loading}
              block
              style={{ height: 44, borderRadius: 8, fontWeight: 600 }}
            >
              {isRegister ? t("login.register") : t("login.submit")}
            </Button>
          </Form.Item>
        </Form>

        <div style={{ textAlign: "center", fontSize: 13 }}>
          {isRegister ? (
            <span>
              {t("login.hasAccount", "已有账号？")}{" "}
              <Button type="link" onClick={() => switchMode(false)}>
                {t("login.backToLogin", "返回登录")}
              </Button>
            </span>
          ) : (
            <span>
              {t("login.noAccount", "还没有账号？")}{" "}
              <Button type="link" onClick={() => switchMode(true)}>
                {t("login.register")}
              </Button>
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
