import services from '@/services/luboman';
import { REQUEST_HOST } from '@/constants';
import {
  CheckCircleOutlined,
  PlusOutlined,
  QrcodeOutlined,
  ReloadOutlined,
  StopOutlined,
  UploadOutlined,
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
  Modal,
  Popconfirm,
  QRCode,
  Select,
  Space,
  Tag,
  Upload,
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
  stopBiliupLogin,
  listBiliAccountUpowerLevels,
} = services.BiliAccount;
const { uploadIntro } = services.Upload;

const loginStatusText: Record<string, string> = {
  created: '已创建',
  waiting: '等待扫码',
  success: '已完成',
  failed: '失败',
  stopped: '已停止',
  expired: '已超时',
};

const loginStatusColor: Record<string, string> = {
  created: 'default',
  waiting: 'processing',
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
  const [biliupLoading, setBiliupLoading] = useState(false);
  // 片头设置 Modal（独立小弹窗，与登录 Modal 状态机隔离）
  const [introAccount, setIntroAccount] = useState<API.BiliAccountInfo>();
  const [introUploading, setIntroUploading] = useState(false);
  const [upowerAccount, setUpowerAccount] = useState<API.BiliAccountInfo>();
  const [upowerLevels, setUpowerLevels] = useState<API.BiliUpowerLevel[]>([]);
  const [upowerSelectedId, setUpowerSelectedId] = useState<string | undefined>();
  const [upowerLoading, setUpowerLoading] = useState(false);
  const [upowerSaving, setUpowerSaving] = useState(false);
  const [upowerHint, setUpowerHint] = useState<string>();

  const handleRemoveIntro = async () => {
    if (!introAccount?.id) return;
    await updateBiliAccount({ id: introAccount.id, intro_video_path: '' });
    message.success('片头已移除');
    setIntroAccount(undefined);
    actionRef.current?.reload();
  };

  const openUpowerModal = async (record: API.BiliAccountInfo) => {
    setUpowerAccount(record);
    setUpowerLevels([]);
    setUpowerSelectedId(record.upower_level_id || undefined);
    setUpowerHint(undefined);
    if (!record.id) return;
    setUpowerLoading(true);
    try {
      const result = await listBiliAccountUpowerLevels(
        { id: record.id },
        { skipErrorHandler: true },
      );
      setUpowerLevels(result.levels || []);
      setUpowerSelectedId(
        result.selected_id || record.upower_level_id || undefined,
      );
      if (!result.levels?.length) {
        setUpowerHint(
          result.message ||
            '未拉取到充电档位。请确认账号已开通充电计划，且存在非 6 元档。',
        );
      }
    } catch (e) {
      setUpowerHint((e as Error)?.message || '拉取充电档位失败');
    } finally {
      setUpowerLoading(false);
    }
  };

  const handleSaveUpower = async () => {
    if (!upowerAccount?.id) return;
    setUpowerSaving(true);
    try {
      await updateBiliAccount({
        id: upowerAccount.id,
        upower_level_id: upowerSelectedId || '',
      });
      message.success(upowerSelectedId ? '充电档位已保存' : '已清除充电档位');
      setUpowerAccount(undefined);
      actionRef.current?.reload();
    } finally {
      setUpowerSaving(false);
    }
  };

  useEffect(() => {
    if (!loginSession?.session_id || loginSession.status !== 'waiting') {
      return undefined;
    }

    const timer = window.setInterval(async () => {
      try {
        const next = await getBiliupLoginStatus(
          { session_id: loginSession.session_id },
          { skipErrorHandler: true },
        );
        setLoginSession(next);
      } catch {
        // 轮询失败时保留当前二维码，统一错误提示会干扰扫码登录流程。
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
    } finally {
      setBiliupLoading(false);
    }
  };

  const handleStopBiliupLogin = async () => {
    if (!loginSession?.session_id) return;
    const session = await stopBiliupLogin({
      session_id: loginSession.session_id,
    });
    setLoginSession(session);
  };

  const resetCreateState = () => {
    form.resetFields();
    setReloginAccount(undefined);
    setLoginSession(undefined);
    setBiliupLoading(false);
  };

  const handleCreateOpenChange = (open: boolean) => {
    if (!open) {
      if (loginSession?.status === 'waiting') {
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
            title: '片头',
            dataIndex: 'intro_video_path',
            width: 90,
            render: (_, r) =>
              r.intro_video_path ? (
                <Tag color="blue">已设置</Tag>
              ) : (
                <Tag>未设置</Tag>
              ),
          },
          {
            title: '充电档位',
            dataIndex: 'upower_level_id',
            width: 100,
            render: (_, r) =>
              r.upower_level_id ? (
                <Tag color="gold">已设置</Tag>
              ) : (
                <Tag>未设置</Tag>
              ),
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
            width: 300,
            render: (_, record) => (
              <Space>
                <a onClick={() => record.id && handleCheckLogin(record.id)}>
                  {checkingKey === record.id ? '检测中' : '检测'}
                </a>
                <a onClick={() => openReloginModal(record)}>重新登录</a>
                <a onClick={() => setIntroAccount(record)}>片头</a>
                <a onClick={() => openUpowerModal(record)}>充电</a>
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
              message.error('请先完成扫码登录');
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
                    disabled={loginSession?.status === 'waiting'}
                    onClick={handleStartBiliupLogin}
                  >
                    启动登录
                  </Button>
                  <Button
                    icon={<StopOutlined />}
                    disabled={loginSession?.status !== 'waiting'}
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
                {loginSession?.qrcode_url ? (
                  <div style={{ textAlign: 'center', padding: '8px 0 16px' }}>
                    <QRCode value={loginSession.qrcode_url} size={180} />
                    <div
                      style={{
                        marginTop: 12,
                        color: 'rgba(255,255,255,0.65)',
                        fontSize: 13,
                      }}
                    >
                      请使用哔哩哔哩 App 扫码登录
                    </div>
                  </div>
                ) : (
                  <div
                    style={{
                      minHeight: 180,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      color: 'rgba(255,255,255,0.45)',
                      fontSize: 13,
                      padding: 12,
                      marginBottom: 12,
                      borderRadius: 6,
                      background: 'rgba(255,255,255,0.04)',
                      border: '1px solid rgba(255,255,255,0.12)',
                    }}
                  >
                    {loginSession
                      ? loginSession.error_message || '正在获取二维码…'
                      : '点击「启动登录」获取二维码'}
                  </div>
                )}
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

      <Modal
        title={`片头设置${introAccount?.account_name ? ` - ${introAccount.account_name}` : ''}`}
        open={!!introAccount}
        onCancel={() => setIntroAccount(undefined)}
        footer={null}
        destroyOnClose
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <div style={{ color: 'rgba(255,255,255,0.65)', fontSize: 13 }}>
            上传片头视频后，该账号所有 B 站投稿的每个视频文件前都会自动拼接片头；不设置则不处理。
          </div>
          <div>
            当前片头：
            {introAccount?.intro_video_path ? (
              <Tag color="blue">
                {introAccount.intro_video_path.split('/').pop()}
              </Tag>
            ) : (
              <Tag>未设置</Tag>
            )}
          </div>
          {introAccount?.intro_video_path ? (
            <video
              src={`${REQUEST_HOST}/public/intro/${introAccount.intro_video_path
                .split('/')
                .pop()}`}
              controls
              style={{ width: '100%', maxHeight: 240, borderRadius: 6 }}
            />
          ) : null}
          <Space>
            <Upload
              accept=".mp4,.flv,.mkv,.ts,.mov"
              showUploadList={false}
              customRequest={async ({ file, onSuccess, onError }) => {
                if (!introAccount?.id) return;
                setIntroUploading(true);
                try {
                  const res = await uploadIntro(file as File);
                  await updateBiliAccount({
                    id: introAccount.id,
                    intro_video_path: res.path,
                  });
                  onSuccess?.(res);
                  message.success('片头已上传并生效');
                  setIntroAccount({
                    ...introAccount,
                    intro_video_path: res.path,
                  });
                  actionRef.current?.reload();
                } catch (e) {
                  // 统一错误层已弹 toast
                  onError?.(e as Error);
                } finally {
                  setIntroUploading(false);
                }
              }}
            >
              <Button
                type="primary"
                loading={introUploading}
                icon={<UploadOutlined />}
              >
                {introAccount?.intro_video_path ? '更换片头' : '上传片头'}
              </Button>
            </Upload>
            {introAccount?.intro_video_path ? (
              <Popconfirm
                title="确认移除片头？"
                description="移除后该账号投稿不再拼接片头"
                onConfirm={handleRemoveIntro}
              >
                <Button danger>移除片头</Button>
              </Popconfirm>
            ) : null}
          </Space>
        </Space>
      </Modal>

      <Modal
        title={`充电档位${upowerAccount?.account_name ? ` - ${upowerAccount.account_name}` : ''}`}
        open={!!upowerAccount}
        onCancel={() => setUpowerAccount(undefined)}
        onOk={handleSaveUpower}
        confirmLoading={upowerSaving}
        okText="保存"
        destroyOnClose
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <div style={{ color: 'rgba(255,255,255,0.65)', fontSize: 13 }}>
            选择该账号发充电专属视频时使用的档位。直播间需另外打开「充电投稿」才会生效；6 元档不能发专属视频。
          </div>
          <Select
            allowClear
            showSearch
            loading={upowerLoading}
            placeholder={upowerLoading ? '正在拉取档位…' : '选择充电档位'}
            value={upowerSelectedId}
            onChange={(value) => setUpowerSelectedId(value)}
            options={(() => {
              const options = (upowerLevels || []).map((level) => ({
                label: level.label || level.name || level.id,
                value: level.id,
                disabled: level.exclusive_ok === false,
              }));
              if (
                upowerSelectedId &&
                !options.some((item) => item.value === upowerSelectedId)
              ) {
                options.unshift({
                  label: `已保存档位 ${upowerSelectedId}`,
                  value: upowerSelectedId,
                  disabled: false,
                });
              }
              return options;
            })()}
            optionFilterProp="label"
            style={{ width: '100%' }}
          />
          {upowerHint ? (
            <div style={{ color: 'rgba(255,255,255,0.55)', fontSize: 13 }}>
              {upowerHint}
              {upowerAccount?.upower_level_id
                ? ` 当前已保存档位 ID：${upowerAccount.upower_level_id}`
                : ''}
            </div>
          ) : null}
        </Space>
      </Modal>
    </>
  );
};

export default BiliAccountList;
