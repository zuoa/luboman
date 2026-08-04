import services from '@/services/luboman';
import {
  PlusOutlined,
  QrcodeOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons';
import {
  ActionType,
  ModalForm,
  ProFormRadio,
  ProFormText,
  ProTable,
} from '@ant-design/pro-components';
import { Avatar, Badge, Button, Form, Popconfirm, Space, Tag, message } from 'antd';
import React, { useEffect, useRef, useState } from 'react';

const {
  listDouyinAccount,
  addDouyinAccount,
  updateDouyinAccount,
  delDouyinAccount,
  startDouyinLogin,
  getDouyinLoginStatus,
  stopDouyinLogin,
} = services.DouyinAccount;

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

const DouyinAccountList: React.FC = () => {
  const actionRef = useRef<ActionType>();
  const [form] = Form.useForm();
  const [createOpen, setCreateOpen] = useState(false);
  const [reloginAccount, setReloginAccount] = useState<API.DouyinAccountInfo>();
  const [loginSession, setLoginSession] = useState<API.DouyinLoginSession>();
  const [loginLoading, setLoginLoading] = useState(false);

  useEffect(() => {
    if (!loginSession?.session_id || loginSession.status !== 'waiting') {
      return undefined;
    }

    const timer = window.setInterval(async () => {
      try {
        const next = await getDouyinLoginStatus(
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
    await delDouyinAccount(id);
    actionRef.current?.reload();
  };

  const handleStartLogin = async () => {
    const values = form.getFieldsValue();
    setLoginLoading(true);
    try {
      const session = await startDouyinLogin({
        douyin_cookies_filepath: values.douyin_cookies_filepath,
        account_name: reloginAccount?.account_name,
      });
      form.setFieldValue('douyin_cookies_filepath', session.cookie_path);
      setLoginSession(session);
    } finally {
      setLoginLoading(false);
    }
  };

  const handleStopLogin = async () => {
    if (!loginSession?.session_id) return;
    const session = await stopDouyinLogin({
      session_id: loginSession.session_id,
    });
    setLoginSession(session);
  };

  const resetCreateState = () => {
    form.resetFields();
    setReloginAccount(undefined);
    setLoginSession(undefined);
    setLoginLoading(false);
  };

  const handleCreateOpenChange = (open: boolean) => {
    if (!open) {
      if (loginSession?.status === 'waiting') {
        stopDouyinLogin(
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
    form.setFieldsValue({ state_active: 1 });
    setCreateOpen(true);
  };

  const openReloginModal = (record: API.DouyinAccountInfo) => {
    resetCreateState();
    setReloginAccount(record);
    form.setFieldsValue({
      state_active: record.state_active ?? 1,
      douyin_cookies_filepath: undefined,
    });
    setCreateOpen(true);
  };

  return (
    <>
      <ProTable<API.DouyinAccountInfo>
        headerTitle="抖音投稿账号"
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
            const data = await listDouyinAccount();
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
            dataIndex: 'douyin_cookies_filepath',
            ellipsis: true,
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
            width: 160,
            render: (_, record) => (
              <Space>
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
        title={reloginAccount ? '重新登录抖音账号' : '新建抖音账号'}
        width={640}
        form={form}
        open={createOpen}
        onOpenChange={handleCreateOpenChange}
        modalProps={{ destroyOnClose: true }}
        initialValues={{ state_active: 1 }}
        onFinish={async (values) => {
          if (loginSession?.status !== 'success' && !values.douyin_cookies_filepath) {
            message.error('请先完成扫码登录');
            return false;
          }
          const payload: Partial<API.DouyinAccountInfo> = {
            account_name: values.account_name,
            state_active: values.state_active,
            douyin_cookies_filepath:
              values.douyin_cookies_filepath || loginSession?.cookie_path,
          };
          if (reloginAccount?.id) {
            await updateDouyinAccount({ id: reloginAccount.id, ...payload });
            message.success('账号登录信息已更新');
          } else {
            await addDouyinAccount(payload);
          }
          actionRef.current?.reload();
          return true;
        }}
      >
        <ProFormText
          name="account_name"
          label="账号名称"
          placeholder="便于区分的名称，如「舞蹈切片号」"
          rules={[{ required: true, message: '请输入账号名称' }]}
        />
        <ProFormText
          name="douyin_cookies_filepath"
          label="Cookies 文件路径"
          placeholder="留空则扫码登录时自动生成"
        />
        <Space style={{ marginBottom: 12 }} wrap>
          <Button
            icon={<QrcodeOutlined />}
            loading={loginLoading}
            disabled={loginSession?.status === 'waiting'}
            onClick={handleStartLogin}
          >
            启动扫码登录
          </Button>
          <Button
            icon={<StopOutlined />}
            disabled={loginSession?.status !== 'waiting'}
            onClick={handleStopLogin}
          >
            停止
          </Button>
          {loginSession ? (
            <Tag color={loginStatusColor[loginSession.status] || 'default'}>
              {loginStatusText[loginSession.status] || loginSession.status}
            </Tag>
          ) : null}
        </Space>
        {loginSession?.qrcode_img ? (
          <div style={{ textAlign: 'center', padding: '8px 0 16px' }}>
            <img
              src={loginSession.qrcode_img}
              alt="抖音登录二维码"
              style={{ width: 180, height: 180 }}
            />
            <div
              style={{
                marginTop: 12,
                color: 'rgba(255,255,255,0.65)',
                fontSize: 13,
              }}
            >
              请使用抖音 App 扫码登录（二维码过期会自动刷新）
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
              : '点击「启动扫码登录」获取二维码'}
          </div>
        )}
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

export default DouyinAccountList;
