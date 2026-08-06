import 'element-plus/dist/index.css'
import '../styles/base.css'
import './chat.css'
import ChatApp from './ChatApp.vue'
import { mountProtectedPage } from '../shared/bootstrap'

mountProtectedPage(ChatApp, 'query')
