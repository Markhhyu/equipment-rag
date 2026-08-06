import 'element-plus/dist/index.css'
import '../styles/base.css'
import './import.css'
import ImportApp from './ImportApp.vue'
import { mountProtectedPage } from '../shared/bootstrap'

mountProtectedPage(ImportApp, 'import')
