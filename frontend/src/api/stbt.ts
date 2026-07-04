import type { StbtConfig, StbtParams, StbtStatusResponse } from '@/types/stbt'
import { webClient } from './client'

interface StbtMutationResponse {
  status: string
  message?: string
  config?: StbtConfig
}

export interface StbtConfigPayload extends Partial<StbtParams> {
  name?: string
  schedule_start?: string
  schedule_stop?: string
  schedule_days?: string[]
}

export const stbtApi = {
  getConfigs: async (): Promise<StbtConfig[]> => {
    const response = await webClient.get<{ configs: StbtConfig[] }>('/stbt/api/configs')
    return response.data.configs || []
  },

  createConfig: async (payload: StbtConfigPayload): Promise<StbtMutationResponse> => {
    const response = await webClient.post<StbtMutationResponse>('/stbt/api/configs', payload)
    return response.data
  },

  updateConfig: async (
    strategyId: string,
    payload: StbtConfigPayload
  ): Promise<StbtMutationResponse> => {
    const response = await webClient.put<StbtMutationResponse>(
      `/stbt/api/configs/${strategyId}`,
      payload
    )
    return response.data
  },

  deleteConfig: async (strategyId: string): Promise<StbtMutationResponse> => {
    const response = await webClient.delete<StbtMutationResponse>(
      `/stbt/api/configs/${strategyId}`
    )
    return response.data
  },

  startConfig: async (strategyId: string): Promise<StbtMutationResponse> => {
    const response = await webClient.post<StbtMutationResponse>(`/stbt/api/start/${strategyId}`, {})
    return response.data
  },

  stopConfig: async (strategyId: string): Promise<StbtMutationResponse> => {
    const response = await webClient.post<StbtMutationResponse>(`/stbt/api/stop/${strategyId}`, {})
    return response.data
  },

  getStatus: async (strategyId: string): Promise<StbtStatusResponse> => {
    const response = await webClient.get<StbtStatusResponse>(`/stbt/api/status/${strategyId}`)
    return response.data
  },
}
