import services from '@/services/luboman';
import {
  CheckCircleOutlined,
  PlusOutlined,
  QrcodeOutlined,
  ReloadOutlined,
  SendOutlined,
  StopOutlined,
} from '@ant-design/icons';
import {
  ActionType,
  ModalForm,
  ProFormDependency,
  ProFormRadio,
  ProFormText,
  ProFormTextArea,
  ProTable,
} from '@ant-design/pro-components';
import {
  Avatar,
  Badge,
  Button,
  Form,
  Input,
  Popconfirm,
  Space,
  Tag,
  message,
} from 'antd';
import React, { useEffect, useRef, useState } from 'react';

const {
  listBiliAccount,
  addBiliAccount,
  updateBiliAccount,
  delBiliAccount,
  checkBiliAccountLogin,
  startBiliupLogin,
  getBiliupLoginStatus,
  sendBiliupLoginInput,
  stopBiliupLogin,
} = services.BiliAccount;

const loginStatusText: Record<string, string> = {
  created: '已创建',
  running: '运行中',
  success: '已完成',
  failed: '失败',
  stopped: '已停止',
  expired: '已超时',
};

const loginStatusColor: Record<string, string> = {
  created: 'default',
  running: 'processing',
  success: 'success',
  failed: 'error',
  stopped: 'default',
  expired: 'warning',
};

const BiliAccountList: React.FC = () => {
  const actionRef = useRef<ActionType>();
  const [form] = Form.useForm();
  const [createOpen, setCreateOpen] = useState(false);
  const [reloginAccount, setReloginAccount] = useState<API.BiliAccountInfo>();
  const [checkingKey, setCheckingKey] = useState<number | 'all'>();
  const [loginChecks, setLoginChecks] = useState<
    Record<number, API.BiliAccountLoginCheckItem>
  >({});
  const [loginSession, setLoginSession] = useState<API.BiliupLoginSession>();
  const [loginOutput, setLoginOutput] = useState<string[]>([]);
  const [loginInput, setLoginInput] = useState('');
  const [biliupLoading, setBiliupLoading] = useState(false);

  useEffect(() => {
    if (!loginSession?.session_id || loginSession.status !== 'running') {
      return undefined;
    }

    const timer = window.setInterval(async () => {
      try {
        const next = await getBiliupLoginStatus(
          { session_id: loginSession.session_id },
          { skipErrorHandler: true },
        );
        setLoginSession(next);
        setLoginOutput(next.output || []);
      } catch {
        // 轮询失败时保留当前输出，统一错误提示会干扰扫码登录流程。
      }
    }, 1200);

    return () => window.clearInterval(timer);
  }, [loginSession?.session_id, loginSession?.status]);

  const handleDelete = async (id: number) => {
    await delBiliAccount(id);
    actionRef.current?.reload();
  };

  const handleCheckLogin = async (id?: number) => {
    setCheckingKey(id || 'all');
    try {
      const result = await checkBiliAccountLogin(id ? { id } : {});
      setLoginChecks((prev) => {
        const next = { ...prev };
        (result.results || []).forEach((item) => {
          if (item.id != null) {
            next[item.id] = item;
          }
        });
        return next;
      });
      if (result.invalid_count > 0) {
        message.warning(`检测完成，失效账号 ${result.invalid_count} 个`);
      } else {
        message.success('检测完成，启用账号登录态有效');
      }
    } finally {
      setCheckingKey(undefined);
    }
  };

  const renderLoginCheck = (record: API.BiliAccountInfo) => {
    if (record.state_active !== 1) {
      return <Badge status="default" text="未启用" />;
    }

    const checked = record.id == null ? undefined : loginChecks[record.id];
    if (!checked) {
      return <Badge status="default" text="未检测" />;
    }
    if (checked.login_valid === true) {
      return <Badge status="success" text="有效" />;
    }
    if (checked.status === 'missing_credentials') {
      return <Badge status="warning" text="缺少 Cookie" />;
    }
    return <Badge status="error" text="失效" />;
  };

  const handleStartBiliupLogin = async () => {
    const values = form.getFieldsValue();
    setBiliupLoading(true);
    try {
      const session = await startBiliupLogin({
        bili_cookies_filepath: values.bili_cookies_filepath,
        account_name: reloginAccount?.account_name,
      });
      form.setFieldValue('bili_cookies_filepath', session.cookie_path);
      setLoginSession(session);
      setLoginOutput(session.output || []);
    } finally {
      setBiliupLoading(false);
    }
  };

  const handleSendLoginInput = async () => {
    const input = loginInput.trim();
    if (!loginSession?.session_id || !input) return;
    const session = await sendBiliupLoginInput({
      session_id: loginSession.session_id,
      input,
    });
    setLoginSession(session);
    setLoginOutput(session.output || []);
    setLoginInput('');
  };

  const handleStopBiliupLogin = async () => {
    if (!loginSession?.session_id) return;
    const session = await stopBiliupLogin({
      session_id: loginSession.session_id,
    });
    setLoginSession(session);
    setLoginOutput(session.output || []);
  };

  const resetCreateState = () => {
    form.resetFields();
    setReloginAccount(undefined);
    setLoginSession(undefined);
    setLoginOutput([]);
    setLoginInput('');
    setBiliupLoading(false);
  };

  const handleCreateOpenChange = (open: boolean) => {
    if (!open) {
      if (loginSession?.status === 'running') {
        stopBiliupLogin(
          { session_id: loginSession.session_id },
          { skipErrorHandler: true },
        ).catch(() => {});
      }
      resetCreateState();
    }
    setCreateOpen(open);
  };

  const openCreateModal = () => {
    resetCreateState();
    form.setFieldsValue({ cookieType: 'biliup', state_active: 1 });
    setCreateOpen(true);
  };

  const openReloginModal = (record: API.BiliAccountInfo) => {
    resetCreateState();
    setReloginAccount(record);
    form.setFieldsValue({
      cookieType: 'biliup',
      state_active: record.state_active ?? 1,
      bili_cookies_filepath: undefined,
      bili_cookies: undefined,
    });
    setCreateOpen(true);
  };

  return (
    <>
      <ProTable<API.BiliAccountInfo>
        headerTitle="B 站投稿账号"
        actionRef={actionRef}
        rowKey="id"
        search={false}
        pagination={false}
        size="middle"
        options={false}
        toolBarRender={() => [
          <Button
            key="reload"
            icon={<ReloadOutlined />}
            onClick={() => actionRef.current?.reload()}
          >
            刷新
          </Button>,
          <Button
            key="check"
            icon={<CheckCircleOutlined />}
            loading={checkingKey === 'all'}
            onClick={() => handleCheckLogin()}
          >
            检测登录态
          </Button>,
          <Button
            key="create"
            type="primary"
            icon={<PlusOutlined />}
            onClick={openCreateModal}
          >
            新建
          </Button>,
        ]}
        request={async () => {
          try {
            const data = await listBiliAccount();
            return { data: data || [], success: true };
          } catch {
            return { data: [], success: false };
          }
        }}
        columns={[
          { title: 'ID', dataIndex: 'id', width: 60 },
          {
            title: '头像',
            dataIndex: 'account_avatar',
            width: 70,
            render: (_, r) =>
              r.account_avatar ? (
                <Avatar src={r.account_avatar} size={32} />
              ) : (
                <Avatar size={32}>{(r.account_name || '?').slice(0, 1)}</Avatar>
              ),
          },
          { title: '账号名', dataIndex: 'account_name', ellipsis: true },
          {
            title: 'Cookies 文件路径',
            dataIndex: 'bili_cookies_filepath',
            ellipsis: true,
          },
          {
            title: '登录态',
            dataIndex: 'login_check',
            width: 120,
            render: (_, r) => renderLoginCheck(r),
          },
          {
            title: '状态',
            dataIndex: 'state_active',
            width: 90,
            render: (_, r) => (
              <Badge
                status={r.state_active === 1 ? 'success' : 'default'}
                text={r.state_active === 1 ? '启用' : '停用'}
              />
            ),
          },
          {
            title: '操作',
            dataIndex: 'option',
            valueType: 'option',
            width: 200,
            render: (_, record) => (
              <Space>
                <a onClick={() => record.id && handleCheckLogin(record.id)}>
                  {checkingKey === record.id ? '检测中' : '检测'}
                </a>
                <a onClick={() => openReloginModal(record)}>重新登录</a>
                <Popconfirm
                  title="确认删除该账号？"
                  description="将停用该账号（state_active 置 0）"
                  onConfirm={() => record.id && handleDelete(record.id)}
                >
                  <a>删除</a>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <ModalForm
        title={reloginAccount ? '重新登录 B 站账号' : '新建 B 站账号'}
        width={640}
        form={form}
        open={createOpen}
        onOpenChange={handleCreateOpenChange}
        modalProps={{ destroyOnClose: true }}
        initialValues={{ cookieType: 'biliup', state_active: 1 }}
        onFinish={async (values) => {
          const payload: Partial<API.BiliAccountInfo> = {
            state_active: values.state_active,
          };
          if (values.cookieType === 'filepath') {
            payload.bili_cookies_filepath = values.bili_cookies_filepath;
          } else if (values.cookieType === 'biliup') {
            if (loginSession?.status !== 'success') {
              message.error('biliup-rs 登录未完成');
              return false;
            }
            payload.bili_cookies_filepath =
              values.bili_cookies_filepath || loginSession.cookie_path;
          } else {
            payload.bili_cookies = values.bili_cookies;
          }
          if (reloginAccount?.id) {
            await updateBiliAccount({ id: reloginAccount.id, ...payload });
            setLoginChecks((prev) => {
              const next = { ...prev };
              delete next[reloginAccount.id as number];
              return next;
            });
            message.success('账号登录信息已更新');
          } else {
            await addBiliAccount(payload);
          }
          actionRef.current?.reload();
          return true;
        }}
      >
        <ProFormRadio.Group
          name="cookieType"
          label="登录方式"
          options={[
            { label: 'biliup-rs 登录', value: 'biliup' },
            { label: 'Cookies 字符串', value: 'cookies' },
            { label: 'Cookies 文件路径', value: 'filepath' },
          ]}
        />
        <ProFormDependency name={['cookieType']}>
          {({ cookieType }) =>
            cookieType === 'filepath' ? (
              <ProFormText
                name="bili_cookies_filepath"
                label="Cookies 文件路径"
                placeholder="biliup-rs 生成的 cookies.json 路径"
                rules={[{ required: true, message: '请输入文件路径' }]}
              />
            ) : cookieType === 'biliup' ? (
              <>
                <ProFormText
                  name="bili_cookies_filepath"
                  label="Cookies 文件路径"
                  placeholder="留空则自动生成"
                />
                <Space style={{ marginBottom: 12 }} wrap>
                  <Button
                    icon={<QrcodeOutlined />}
                    loading={biliupLoading}
                    disabled={loginSession?.status === 'running'}
                    onClick={handleStartBiliupLogin}
                  >
                    启动登录
                  </Button>
                  <Button
                    icon={<StopOutlined />}
                    disabled={loginSession?.status !== 'running'}
                    onClick={handleStopBiliupLogin}
                  >
                    停止
                  </Button>
                  {loginSession ? (
                    <Tag
                      color={loginStatusColor[loginSession.status] || 'default'}
                    >
                      {loginStatusText[loginSession.status] ||
                        loginSession.status}
                    </Tag>
                  ) : null}
                </Space>
                <Input.Search
                  value={loginInput}
                  placeholder="输入"
                  enterButton={<SendOutlined />}
                  disabled={loginSession?.status !== 'running'}
                  onChange={(event) => setLoginInput(event.target.value)}
                  onSearch={handleSendLoginInput}
                  style={{ marginBottom: 12 }}
                />
                <pre
                  style={{
                    minHeight: 180,
                    maxHeight: 300,
                    overflow: 'auto',
                    padding: 12,
                    borderRadius: 6,
                    background: 'rgba(255,255,255,0.04)',
                    border: '1px solid rgba(255,255,255,0.12)',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                  }}
                >
                  {loginOutput.join('\n')}
                </pre>
              </>
            ) : (
              <ProFormTextArea
                name="bili_cookies"
                label="Cookies 字符串"
                placeholder="SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx;"
                rules={[{ required: true, message: '请输入 Cookies' }]}
                fieldProps={{ autoSize: { minRows: 3, maxRows: 6 } }}
              />
            )
          }
        </ProFormDependency>
        <ProFormRadio.Group
          name="state_active"
          label="状态"
          options={[
            { label: '启用', value: 1 },
            { label: '停用', value: 0 },
          ]}
        />
      </ModalForm>
    </>
  );
};

export default BiliAccountList;
