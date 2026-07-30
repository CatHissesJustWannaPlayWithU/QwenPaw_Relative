import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Avatar,
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Result,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Upload,
} from "antd";
import type { UploadFile } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  Pencil,
  Plus,
  Power,
  Trash2,
  Upload as UploadIcon,
  UserRound,
} from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { useAppMessage } from "../../../hooks/useAppMessage";
import {
  type UserProfile,
  type UserRole,
  type UserStatus,
} from "../../../api/modules/auth";
import {
  type CreateUserPayload,
  type UpdateUserPayload,
  usersApi,
} from "../../../api/modules/users";
import { useAuthStore } from "../../../stores/authStore";
import styles from "./index.module.less";

//新增和编辑弹窗读取的字段。password 在编辑时可留空，表示不修改密码。
interface UserFormValues {
  username: string;
  password?: string;
  name: string;
  role: UserRole;
  status: UserStatus;
}

function readFileAsDataUrl(file: File): Promise<string> {
  //头像作为 Data URL 随 JSON 请求提交，因此不需要额外的文件上传接口。
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("Failed to read avatar"));
    reader.readAsDataURL(file);
  });
}

function formatCreatedAt(value: string): string {
  //后端时间格式异常时保留原文，避免格式化失败导致表格无法渲染。
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function UsersPage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const currentUser = useAuthStore((state) => state.user); //用于前端显示权限判断和保护当前管理员自身。
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editingUser, setEditingUser] = useState<UserProfile | null>(null);
  const [avatar, setAvatar] = useState("");
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [form] = Form.useForm<UserFormValues>();

  const loadUsers = async () => {
    //表格首次加载和需要重新获取数据时，统一通过 usersApi.list 请求用户列表。
    setLoading(true);
    try {
      const response = await usersApi.list();
      setUsers(response.users);
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("users.loadFailed"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    //只有已确认的管理员才发起列表请求，避免普通用户无意义地调用管理接口。
    if (currentUser?.role === "admin") {
      void loadUsers();
    }
  }, [currentUser?.role]);

  const openCreate = () => {
    //新增模式不带任何旧用户数据，并设置普通用户、启用状态为默认值。
    setEditingUser(null);
    setAvatar("");
    setFileList([]);
    form.resetFields();
    form.setFieldsValue({ role: "user", status: "active" });
    setModalOpen(true);
  };

  const openEdit = (user: UserProfile) => {
    //编辑模式把被选中用户的数据回填到同一个弹窗；密码始终留空，除非管理员主动重置。
    setEditingUser(user);
    setAvatar(user.avatar || "");
    setFileList([]);
    form.setFieldsValue({
      username: user.username,
      name: user.name,
      role: user.role,
      status: user.status,
      password: undefined,
    });
    setModalOpen(true);
  };

  const handleSave = async () => {
    //validateFields 先执行 Ant Design 表单规则，再决定调用新增还是修改接口。
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (editingUser) {
        const payload: UpdateUserPayload = {
          username: values.username.trim(),
          name: values.name.trim(),
          avatar,
          role: values.role,
          status: values.status,
        };
        if (values.password?.trim()) {
          //空密码不能覆盖原密码，只有管理员明确填写时才发送 password 字段。
          payload.password = values.password.trim();
        }
        const updated = await usersApi.update(editingUser.id, payload);
        //用新对象替换表格中对应行，保持 React 状态不可变更新。
        setUsers((items) =>
          items.map((item) => (item.id === updated.id ? updated : item)),
        );
        message.success(t("users.updateSuccess"));
      } else {
        const payload: CreateUserPayload = {
          username: values.username.trim(),
          password: values.password?.trim() || "",
          name: values.name.trim(),
          avatar,
          role: values.role,
          status: values.status,
        };
        const created = await usersApi.create(payload);
        //新增成功后直接追加返回的用户，无需再发一次列表请求。
        setUsers((items) => [...items, created]);
        message.success(t("users.createSuccess"));
      }
      setModalOpen(false);
      form.resetFields();
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("users.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = (user: UserProfile) => {
    //删除是不可逆操作，先让管理员在确认框中再次确认。
    Modal.confirm({
      title: t("users.deleteTitle"),
      content: t("users.deleteConfirm", { name: user.name }),
      okText: t("common.delete"),
      okType: "danger",
      cancelText: t("common.cancel"),
      onOk: async () => {
        try {
          await usersApi.remove(user.id);
          setUsers((items) => items.filter((item) => item.id !== user.id));
          message.success(t("users.deleteSuccess"));
        } catch (error) {
          message.error(
            error instanceof Error ? error.message : t("users.deleteFailed"),
          );
        }
      },
    });
  };

  const handleToggleStatus = async (user: UserProfile) => {
    //启用和停用复用修改接口，只提交 status 这个发生变化的字段。
    try {
      const updated = await usersApi.update(user.id, {
        status: user.status === "active" ? "disabled" : "active",
      });
      setUsers((items) =>
        items.map((item) => (item.id === updated.id ? updated : item)),
      );
      message.success(t("users.statusUpdated"));
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("users.saveFailed"));
    }
  };

  const columns = useMemo<ColumnsType<UserProfile>>(
    //列定义依赖当前登录用户和多语言函数，依赖不变时复用计算结果。
    () => [
      {
        title: t("users.userId"),
        dataIndex: "id",
        width: 140,
        render: (id: string) => <span className={styles.userId}>{id}</span>,
      },
      {
        title: t("users.user"),
        key: "user",
        width: 220,
        render: (_, user) => (
          <Space>
            <Avatar
              src={user.avatar || undefined}
              icon={!user.avatar ? <UserRound size={16} /> : undefined}
            />
            <span className={styles.userCell}>
              <strong>{user.name}</strong>
              <span>{user.username}</span>
            </span>
          </Space>
        ),
      },
      {
        title: t("users.role"),
        dataIndex: "role",
        width: 110,
        render: (role: UserRole) => (
          <Tag color={role === "admin" ? "orange" : "blue"}>
            {role === "admin"
              ? t("users.admin")
              : t("users.normalUser")}
          </Tag>
        ),
      },
      {
        title: t("users.status"),
        dataIndex: "status",
        width: 100,
        render: (status: UserStatus) => (
          <Tag color={status === "active" ? "success" : "default"}>
            {status === "active"
              ? t("users.active")
              : t("users.disabled")}
          </Tag>
        ),
      },
      {
        title: t("users.createdAt"),
        dataIndex: "created_at",
        width: 160,
        render: (createdAt: string) => formatCreatedAt(createdAt),
      },
      {
        title: t("common.actions"),
        key: "actions",
        width: 190,
        render: (_, user) => (
          <Space size={4}>
            <Button
              type="text"
              size="small"
              icon={<Pencil size={15} />}
              onClick={() => openEdit(user)}
            >
              {t("common.edit")}
            </Button>
            <Button
              type="text"
              size="small"
              icon={<Power size={15} />}
              disabled={user.id === currentUser?.id}
              onClick={() => void handleToggleStatus(user)}
            >
              {user.status === "active"
                ? t("users.disable")
                : t("users.enable")}
            </Button>
            <Button
              type="text"
              danger
              size="small"
              icon={<Trash2 size={15} />}
              disabled={user.id === currentUser?.id}
              onClick={() => handleDelete(user)}
            >
              {t("common.delete")}
            </Button>
          </Space>
        ),
      },
    ],
    [currentUser?.id, t],
  );

  if (!currentUser) {
    //AuthGuard 正在恢复用户资料时，页面先显示加载状态。
    return <Spin className={styles.centered} />;
  }

  if (currentUser.role !== "admin") {
    //菜单隐藏不能替代权限保护；直接访问 /users 的普通用户仍会看到 403 页面。
    return (
      <Result
        status="403"
        title={t("users.forbiddenTitle")}
        subTitle={t("users.forbiddenDescription")}
      />
    );
  }

  return (
    <div className={styles.page}>
      <PageHeader
        parent={t("nav.settings")}
        current={t("nav.users", "用户管理")}
        extra={
          <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
            {t("users.add")}
          </Button>
        }
      />
      <div className={styles.content}>
        <div className={styles.intro}>
          <div>
            <h2>{t("users.title")}</h2>
            <p>{t("users.description")}</p>
          </div>
          <span>{t("users.total", { count: users.length })}</span>
        </div>
        <Spin spinning={loading}>
          {users.length === 0 && !loading ? (
            <Empty description={t("users.empty")} />
          ) : (
            <Table
              rowKey="id"
              dataSource={users}
              columns={columns}
              pagination={{ pageSize: 10, showSizeChanger: false }}
              className={styles.table}
              scroll={{ x: 920 }}
            />
          )}
        </Spin>
      </div>

      <Modal
        open={modalOpen}
        title={editingUser ? t("users.editTitle") : t("users.addTitle")}
        onCancel={() => setModalOpen(false)}
        onOk={() => void handleSave()}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        confirmLoading={saving}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" className={styles.form}>
          <Form.Item label={t("users.avatar")}>
            <Space>
              <Avatar
                size={56}
                src={avatar || undefined}
                icon={!avatar ? <UserRound size={18} /> : undefined}
              />
              <Upload
                accept="image/*"
                maxCount={1}
                fileList={fileList}
                beforeUpload={async (file) => {
                  const dataUrl = await readFileAsDataUrl(file);
                  setAvatar(dataUrl);
                  setFileList([file]);
                  return false;
                }}
                onRemove={() => {
                  setAvatar("");
                  setFileList([]);
                }}
                showUploadList={{ showPreviewIcon: false }}
              >
                <Button icon={<UploadIcon size={15} />}>
                  {t("users.chooseAvatar")}
                </Button>
              </Upload>
            </Space>
          </Form.Item>
          <Form.Item
            name="name"
            label={t("users.name")}
            rules={[{ required: true, message: t("users.nameRequired") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="username"
            label={t("users.username")}
            rules={[{ required: true, message: t("users.usernameRequired") }]}
          >
            <Input disabled={editingUser?.id === currentUser.id} />
          </Form.Item>
          <Form.Item
            name="password"
            label={editingUser ? t("users.resetPassword") : t("users.password")}
            rules={
              editingUser
                ? []
                : [{ required: true, min: 8, message: t("users.passwordRequired") }]
            }
          >
            <Input.Password
              placeholder={editingUser ? t("users.passwordOptional") : undefined}
            />
          </Form.Item>
          <Space className={styles.formRow} align="start">
            <Form.Item name="role" label={t("users.role")} rules={[{ required: true }]}>
              <Select
                style={{ width: 180 }}
                options={[
                  { value: "user", label: t("users.normalUser") },
                  { value: "admin", label: t("users.admin") },
                ]}
              />
            </Form.Item>
            <Form.Item name="status" label={t("users.status")} rules={[{ required: true }]}>
              <Select
                style={{ width: 180 }}
                options={[
                  { value: "active", label: t("users.active") },
                  { value: "disabled", label: t("users.disabled") },
                ]}
              />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </div>
  );
}
